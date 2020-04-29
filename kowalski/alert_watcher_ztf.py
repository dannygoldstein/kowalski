import argparse
from ast import literal_eval
from astropy.io import fits
import base64
from bson.json_util import dumps, loads
import confluent_kafka
from copy import deepcopy
import datetime
import fastavro
import gzip
import io
from matplotlib.colors import LogNorm
import matplotlib.pyplot as plt
import multiprocessing
import numpy as np
import os
import pandas as pd
import pymongo
import requests
# from requests.adapters import HTTPAdapter, DEFAULT_POOLBLOCK, DEFAULT_POOLSIZE, DEFAULT_RETRIES
import subprocess
import sys
from tensorflow.keras.models import load_model
import time
import traceback

from utils import deg2dms, deg2hms, great_circle_distance, in_ellipse, load_config, radec2lb, time_stamp


''' load config and secrets '''
config = load_config(config_file='config_ingester.json')


''' Utilities for manipulating Avro data and schemas. '''


def writeAvroData(json_data, json_schema):
    """Encode json into Avro format given a schema.

    Parameters
    ----------
    json_data : `dict`
        The JSON data containing message content.
    json_schema : `dict`
        The writer Avro schema for encoding data.

    Returns
    -------
    `_io.BytesIO`
        Encoded data.
    """
    bytes_io = io.BytesIO()
    fastavro.schemaless_writer(bytes_io, json_schema, json_data)
    return bytes_io


def readAvroData(bytes_io, json_schema):
    """Read data and decode with a given Avro schema.

    Parameters
    ----------
    bytes_io : `_io.BytesIO`
        Data to be decoded.
    json_schema : `dict`
        The reader Avro schema for decoding data.

    Returns
    -------
    `dict`
        Decoded data.
    """
    bytes_io.seek(0)
    message = fastavro.schemaless_reader(bytes_io, json_schema)
    return message


def readSchemaData(bytes_io):
    """Read data that already has an Avro schema.

    Parameters
    ----------
    bytes_io : `_io.BytesIO`
        Data to be decoded.

    Returns
    -------
    `dict`
        Decoded data.
    """
    bytes_io.seek(0)
    message = fastavro.reader(bytes_io)
    return message


class AlertError(Exception):
    """
        Base class for exceptions in this module.
    """
    pass


class EopError(AlertError):
    """
        Exception raised when reaching end of partition.

    Parameters
    ----------
    msg : Kafka message
        The Kafka message result from consumer.poll().
    """
    def __init__(self, msg):
        message = f'{time_stamp()}: topic:{msg.topic()}, partition:{msg.partition()}, '\
                  f'status:end, offset:{msg.offset()}, key:{str(msg.key())}\n'
        self.message = message

    def __str__(self):
        return self.message


class AlertConsumer(object):
    """
        Creates an alert stream Kafka consumer for a given topic.

    Parameters
    ----------
    topic : `str`
        Name of the topic to subscribe to.
    schema_files : Avro schema files
        The reader Avro schema files for decoding data. Optional.
    **kwargs
        Keyword arguments for configuring confluent_kafka.Consumer().
    """

    def __init__(self, topic, **kwargs):

        # keep track of disconnected partitions
        self.num_disconnected_partitions = 0
        self.topic = topic

        def error_cb(err, _self=self):
            print(f'{time_stamp()}: error_cb -------->', err)
            # print(err.code())
            if err.code() == -195:
                _self.num_disconnected_partitions += 1
                if _self.num_disconnected_partitions == _self.num_partitions:
                    print(f'{time_stamp()}: all partitions got disconnected, killing thread')
                    sys.exit()
                else:
                    print(f'{time_stamp()}: {_self.topic}: disconnected from partition.',
                          'total:', _self.num_disconnected_partitions)

        # 'error_cb': error_cb
        kwargs['error_cb'] = error_cb

        self.consumer = confluent_kafka.Consumer(**kwargs)
        self.num_partitions = 0

        def on_assign(consumer, partitions, _self=self):
            # force-reset offsets when subscribing to a topic:
            for part in partitions:
                # -2 stands for beginning and -1 for end
                part.offset = -2
                # keep number of partitions. when reaching end of last partition, kill thread and start from beginning
                _self.num_partitions += 1
                print(consumer.get_watermark_offsets(part))

        self.consumer.subscribe([topic], on_assign=on_assign)

        self.config = config

        # session to talk to SkyPortal
        self.session = requests.Session()
        self.session_headers = {'Authorization': f"token {config['skyportal']['token']}"}
        # non-default settings:
        # pc = pool_connections if pool_connections is not None else DEFAULT_POOLSIZE
        # pm = pool_maxsize if pool_maxsize is not None else DEFAULT_POOLSIZE
        # mr = max_retries if max_retries is not None else DEFAULT_RETRIES
        # pb = pool_block if pool_block is not None else DEFAULT_POOLBLOCK
        #
        # self.session.mount('https://', HTTPAdapter(pool_connections=pc, pool_maxsize=pm,
        #                                            max_retries=mr, pool_block=pb))
        # self.session.mount('http://', HTTPAdapter(pool_connections=pc, pool_maxsize=pm,
        #                                           max_retries=mr, pool_block=pb))

        # MongoDB collections to store the alerts:
        self.collection_alerts = self.config['database']['collection_alerts_ztf']
        self.collection_alerts_aux = self.config['database']['collection_alerts_ztf_aux']

        self.db = None
        self.connect_to_db()

        # create indexes
        # print(self.config['indexes'][self.collection_alerts])
        for index_name, index in self.config['indexes'][self.collection_alerts].items():
            # print(index_name, index)
            ind = [tuple(ii) for ii in index]
            self.db['db'][self.collection_alerts].create_index(keys=ind, name=index_name, background=True)

        # ML models:
        self.ml_models = dict()
        for m in config['ml_models']:
            try:
                m_v = config["ml_models"][m]["version"]
                mf = os.path.join(config["path"]["path_ml_models"], f'{m}_{m_v}.h5')
                self.ml_models[m] = {'model': load_model(mf), 'version': m_v}
            except Exception as e:
                print(f'{time_stamp()}: Error loading ML model {m}: {str(e)}')
                _err = traceback.format_exc()
                print(_err)
                continue

        # filter pipeline upstream: select current alert, ditch cutouts, and merge with aux data
        # including archival photometry and cross-matches:
        self.filter_pipeline_upstream = config['filters'][self.collection_alerts]
        print('Upstream filtering pipeline:')
        print(self.filter_pipeline_upstream)

        # load user-defined alert filter templates
        # todo: implement magic variables such as <jd>, <jd_date>
        # load only the latest filter for each group_id. the assumption is that there is only one filter per group_id
        # as far as the end user is concerned
        # self.filter_templates = \
        #     list(self.db['db'][config['database']['collection_filters']].find({'catalog': self.collection_alerts}))
        self.filter_templates = list(self.db['db'][config['database']['collection_filters']].\
            aggregate([{'$match': {'catalog': self.collection_alerts}},
                       {'$group': {'_id': 'science_program_id',
                                   'created': {'$max': '$created'}, 'tmp': {'$last': '$$ROOT'}}},
                       {'$group': {'_id': None, "filters": {"$push": "$tmp"}}}]))
        if len(self.filter_templates) > 0:
            self.filter_templates = self.filter_templates[0]['filters']

        print('Science filters:')
        print(self.filter_templates)

        # prepend default upstream filter:
        for filter_template in self.filter_templates:
            filter_template['pipeline'] = self.filter_pipeline_upstream + loads(filter_template['pipeline'])

    def connect_to_db(self):
        """
            Connect to mongodb
        :return:
        """

        try:
            # there's only one instance of DB, it's too big to be replicated
            _client = pymongo.MongoClient(host=self.config['database']['host'],
                                          port=self.config['database']['port'], connect=False)
            # grab main database:
            _db = _client[self.config['database']['db']]
        except Exception as _e:
            raise ConnectionRefusedError
        try:
            # authenticate
            _db.authenticate(self.config['database']['username'], self.config['database']['password'])
        except Exception as _e:
            raise ConnectionRefusedError

        self.db = dict()
        self.db['client'] = _client
        self.db['db'] = _db

    def insert_db_entry(self, _collection=None, _db_entry=None):
        """
            Insert a document _doc to collection _collection in DB.
            It is monitored for timeout in case DB connection hangs for some reason
        :param _collection:
        :param _db_entry:
        :return:
        """
        assert _collection is not None, 'Must specify collection'
        assert _db_entry is not None, 'Must specify document'
        try:
            self.db['db'][_collection].insert_one(_db_entry)
        except Exception as _e:
            print(f'{time_stamp()}: Error inserting {str(_db_entry["_id"])} into {_collection}')
            traceback.print_exc()
            print(_e)

    def insert_multiple_db_entries(self, _collection=None, _db_entries=None):
        """
            Insert a document _doc to collection _collection in DB.
            It is monitored for timeout in case DB connection hangs for some reason
        :param _collection:
        :param _db_entries:
        :return:
        """
        assert _collection is not None, 'Must specify collection'
        assert _db_entries is not None, 'Must specify documents'
        try:
            # ordered=False ensures that every insert operation will be attempted
            # so that if, e.g., a document already exists, it will be simply skipped
            self.db['db'][_collection].insert_many(_db_entries, ordered=False)
        except pymongo.errors.BulkWriteError as bwe:
            print(time_stamp(), bwe.details)
        except Exception as _e:
            traceback.print_exc()
            print(_e)

    def replace_db_entry(self, _collection=None, _filter=None, _db_entry=None):
        """
            Insert a document _doc to collection _collection in DB.
            It is monitored for timeout in case DB connection hangs for some reason
        :param _collection:
        :param _filter:
        :param _db_entry:
        :return:
        """
        assert _collection is not None, 'Must specify collection'
        assert _db_entry is not None, 'Must specify document'
        try:
            self.db['db'][_collection].replace_one(_filter, _db_entry, upsert=True)
        except Exception as _e:
            print(time_stamp(), 'Error replacing {:s} in {:s}'.format(str(_db_entry['_id']), _collection))
            traceback.print_exc()
            print(_e)

    @staticmethod
    def alert_mongify(alert):

        doc = dict(alert)

        # let mongo create a unique _id

        # placeholders for classifications
        doc['classifications'] = dict()

        '''Coordinates:'''

        # GeoJSON for 2D indexing
        doc['coordinates'] = {}
        _ra = doc['candidate']['ra']
        _dec = doc['candidate']['dec']
        _radec = [_ra, _dec]
        # string format: H:M:S, D:M:S
        # tic = time.time()
        _radec_str = [deg2hms(_ra), deg2dms(_dec)]
        # print(time.time() - tic)
        # print(_radec_str)
        doc['coordinates']['radec_str'] = _radec_str
        # for GeoJSON, must be lon:[-180, 180], lat:[-90, 90] (i.e. in deg)
        _radec_geojson = [_ra - 180.0, _dec]
        doc['coordinates']['radec_geojson'] = {'type': 'Point',
                                               'coordinates': _radec_geojson}
        # radians and degrees:
        # doc['coordinates']['radec_rad'] = [_ra * np.pi / 180.0, _dec * np.pi / 180.0]
        # doc['coordinates']['radec_deg'] = [_ra, _dec]

        # Galactic coordinates l and b
        l, b = radec2lb(doc['candidate']['ra'], doc['candidate']['dec'])
        doc['coordinates']['l'] = l
        doc['coordinates']['b'] = b

        prv_candidates = deepcopy(doc['prv_candidates'])
        doc.pop('prv_candidates', None)
        if prv_candidates is None:
            prv_candidates = []

        return doc, prv_candidates

    def poll(self, path_alerts=None, path_tess=None, datestr=None, save_packets=True):
        """
            Polls Kafka broker to consume topic.
        :param path_alerts:
        :param path_tess:
        :param datestr:
        :param save_packets:
        :return:
        """
        # msg = self.consumer.poll(timeout=timeout)
        msg = self.consumer.poll()

        if msg is None:
            print(time_stamp(), 'Caught error: msg is None')

        if msg.error():
            print(time_stamp(), 'Caught error:', msg.error())
            raise EopError(msg)

        elif msg is not None:
            try:
                # decode avro packet
                msg_decoded = self.decodeMessage(msg)
                for record in msg_decoded:

                    candid = record['candid']
                    objectId = record['objectId']

                    print(f'{time_stamp()}: {self.topic} {objectId} {candid}')

                    # check that candid not in collection_alerts
                    if self.db['db'][self.collection_alerts].count_documents({'candid': candid}, limit=1) == 0:
                        # candid not in db, ingest

                        if save_packets:
                            # save avro packet to disk
                            path_alert_dir = os.path.join(path_alerts, datestr)
                            # mkdir if does not exist
                            if not os.path.exists(path_alert_dir):
                                os.makedirs(path_alert_dir)
                            path_avro = os.path.join(path_alert_dir, f'{candid}.avro')
                            print(f'{time_stamp()}: saving {candid} to disk')
                            with open(path_avro, 'wb') as f:
                                f.write(msg.value())

                        # ingest decoded avro packet into db
                        # todo: ?? restructure alerts even further?
                        #       move cutouts to ZTF_alerts_cutouts? reduce the main db size for performance
                        #       group by objectId similar to prv_candidates?? maybe this is too much
                        alert, prv_candidates = self.alert_mongify(record)

                        # ML models:
                        scores = alert_filter__ml(record, ml_models=self.ml_models)
                        alert['classifications'] = scores

                        print(f'{time_stamp()}: ingesting {alert["candid"]} into db')
                        self.insert_db_entry(_collection=self.collection_alerts, _db_entry=alert)

                        # prv_candidates: pop nulls - save space
                        prv_candidates = [{kk: vv for kk, vv in prv_candidate.items() if vv is not None}
                                          for prv_candidate in prv_candidates]

                        # cross-match with external catalogs if objectId not in collection_alerts_aux:
                        if self.db['db'][self.collection_alerts_aux].count_documents({'_id': objectId}, limit=1) == 0:
                            # tic = time.time()
                            xmatches = alert_filter__xmatch(self.db['db'], alert)
                            # CLU cross-match:
                            xmatches = {**xmatches, **alert_filter__xmatch_clu(self.db['db'], alert)}
                            # alert['cross_matches'] = xmatches
                            # toc = time.time()
                            # print(f'xmatch for {alert["candid"]} took {toc-tic:.2f} s')

                            alert_aux = {'_id': objectId,
                                         'cross_matches': xmatches,
                                         'prv_candidates': prv_candidates}

                            self.insert_db_entry(_collection=self.collection_alerts_aux, _db_entry=alert_aux)

                        else:
                            self.db['db'][self.collection_alerts_aux].update_one({'_id': objectId},
                                                                                 {'$addToSet':
                                                                                      {'prv_candidates':
                                                                                           {'$each': prv_candidates}}},
                                                                                 upsert=True)

                        # dump packet as json to disk if in a public TESS sector
                        if 'TESS' in alert['candidate']['programpi']:
                            # put prv_candidates back
                            alert['prv_candidates'] = prv_candidates

                            # get cross-matches
                            # xmatches = self.db['db'][self.collection_alerts_aux].find_one({'_id': objectId})
                            xmatches = self.db['db'][self.collection_alerts_aux].find({'_id': objectId},
                                                                                      {'cross_matches': 1},
                                                                                      limit=1)
                            xmatches = list(xmatches)[0]
                            # fixme: pop CLU:
                            xmatches.pop('CLU_20190625', None)

                            alert['cross_matches'] = xmatches['cross_matches']

                            if save_packets:
                                path_tess_dir = os.path.join(path_tess, datestr)
                                # mkdir if does not exist
                                if not os.path.exists(path_tess_dir):
                                    os.makedirs(path_tess_dir)

                                print(f'{time_stamp()}: saving {alert["candid"]} to disk')
                                try:
                                    with open(os.path.join(path_tess_dir, f"{alert['candid']}.json"), 'w') as f:
                                        f.write(dumps(alert))
                                except Exception as e:
                                    print(f'{time_stamp()}: {str(e)}')
                                    _err = traceback.format_exc()
                                    print(f'{time_stamp()}: {str(_err)}')

                        # execute user-defined alert filters
                        passed_filters = alert_filter__user_defined(self.db['db'], self.filter_templates, alert)

                        # fixme: for an early demo, post the alerts directly to /api/sources (change to /api/candidates)
                        #        eventually, should only post passed_filters to a fritz-specific endpoint

                        # if config['misc']['post_to_skyportal'] and (len(passed_filters) > 0):
                        if config['misc']['post_to_skyportal']:
                            # post metadata
                            # fixme: pass annotations, cross-matches, ml scores etc.
                            alert_thin = {
                                "id": alert['objectId'],
                                "ra": alert['candidate'].get('ra'),
                                "dec": alert['candidate'].get('dec'),
                                # 'dist_nearest_source': alert["candidate"].get("distnr"),
                                # 'mag_nearest_source': alert["candidate"].get("magnr"),
                                # 'e_mag_nearest_source': alert["candidate"].get("sigmagnr"),
                                # 'sgmag1': alert["candidate"].get("sgmag1"),
                                # 'srmag1': alert["candidate"].get("srmag1"),
                                # 'simag1': alert["candidate"].get("simag1"),
                                # 'objectidps1': alert["candidate"].get("objectidps1"),
                                # 'sgscore1': alert["candidate"].get("sgscore1"),
                                # 'distpsnr1': alert["candidate"].get("distpsnr1"),
                                "score": alert['candidate'].get('drb', alert['candidate']['rb']),
                            }

                            # fixme: delete the source for a clean start
                            # fixme: this is for the early demo only
                            # resp = self.session.delete(
                            #     f"{config['skyportal']['protocol']}://"
                            #     f"{config['skyportal']['host']}:{config['skyportal']['port']}"
                            #     f"/api/sources/{alert['objectId']}",
                            #     headers=self.session_headers, timeout=2,
                            # )

                            resp = self.session.post(
                                f"{config['skyportal']['protocol']}://"
                                f"{config['skyportal']['host']}:{config['skyportal']['port']}"
                                "/api/sources",
                                json=alert_thin, headers=self.session_headers, timeout=1,
                            )
                            print(f"{time_stamp()}: Posted {alert['candid']} metadata to SkyPortal")
                            print(resp.json())

                            # post photometry
                            alert['prv_candidates'] = prv_candidates
                            photometry = make_photometry(deepcopy(alert))

                            resp = self.session.post(
                                f"{config['skyportal']['protocol']}://"
                                f"{config['skyportal']['host']}:{config['skyportal']['port']}"
                                "/api/photometry",
                                json=photometry, headers=self.session_headers, timeout=1,
                            )
                            print(f"{time_stamp()}: Posted {alert['candid']} photometry to SkyPortal")
                            print(resp.json())

                            # post thumbnails
                            for ttype, ztftype in [('new', 'Science'), ('ref', 'Template'), ('sub', 'Difference')]:

                                thumb = make_thumbnail(deepcopy(alert), ttype, ztftype)

                                resp = self.session.post(
                                    f"{config['skyportal']['protocol']}://"
                                    f"{config['skyportal']['host']}:{config['skyportal']['port']}"
                                    "/api/thumbnail",
                                    json=thumb, headers=self.session_headers, timeout=1,
                                )
                                print(f"{time_stamp()}: Posted {alert['candid']} {ztftype} cutout to SkyPortal")
                                print(resp.json())

            except Exception as e:
                print(f"{time_stamp()}: {str(e)}")

    def decodeMessage(self, msg):
        """Decode Avro message according to a schema.

        Parameters
        ----------
        msg : Kafka message
            The Kafka message result from consumer.poll().

        Returns
        -------
        `dict`
            Decoded message.
        """
        # print(msg.topic(), msg.offset(), msg.error(), msg.key(), msg.value())
        message = msg.value()
        # print(message)
        try:
            bytes_io = io.BytesIO(message)
            decoded_msg = readSchemaData(bytes_io)
            # print(decoded_msg)
            # decoded_msg = readAvroData(bytes_io, self.alert_schema)
            # print(decoded_msg)
        except AssertionError:
            # FIXME this exception is raised but not sure if it matters yet
            bytes_io = io.BytesIO(message)
            decoded_msg = None
        except IndexError:
            literal_msg = literal_eval(str(message, encoding='utf-8'))  # works to give bytes
            bytes_io = io.BytesIO(literal_msg)  # works to give <class '_io.BytesIO'>
            decoded_msg = readSchemaData(bytes_io)  # yields reader
        except Exception:
            decoded_msg = message
        finally:
            return decoded_msg


def make_photometry(a):
    df = pd.DataFrame(a['candidate'], index=[0])

    df_prv = pd.DataFrame(a['prv_candidates'])
    dflc = pd.concat(
        [df, df_prv],
        ignore_index=True,
        sort=False
    ).drop_duplicates(subset='jd').reset_index(drop=True).sort_values(by=['jd']).fillna(99)

    ztf_filters = {1: 'g', 2: 'r', 3: 'i'}
    dflc['ztf_filter'] = dflc['fid'].apply(lambda x: ztf_filters[x])

    photometry = {
        "source_id": a['objectId'],
        "time_format": "jd",
        "time_scale": "utc",
        "instrument_id": 1,
        "observed_at": dflc.jd.tolist(),
        "mag": dflc.magpsf.tolist(),
        "e_mag": dflc.sigmapsf.tolist(),
        "lim_mag": dflc.diffmaglim.tolist(),
        "filter": dflc.ztf_filter.tolist(),
    }

    return photometry


def make_thumbnail(a, ttype, ztftype):

    cutout_data = a[f'cutout{ztftype}']['stampData']
    with gzip.open(io.BytesIO(cutout_data), 'rb') as f:
        with fits.open(io.BytesIO(f.read())) as hdu:
            header = hdu[0].header
            data_flipped_y = np.flipud(hdu[0].data)
    # fixme: png, switch to fits eventually
    buff = io.BytesIO()
    plt.close('all')
    fig = plt.figure()
    fig.set_size_inches(4, 4, forward=False)
    ax = plt.Axes(fig, [0., 0., 1., 1.])
    ax.set_axis_off()
    fig.add_axes(ax)

    # remove nans:
    img = np.array(data_flipped_y)
    img = np.nan_to_num(img)

    if ztftype != 'Difference':
        # img += np.min(img)
        img[img <= 0] = np.median(img)
        # plt.imshow(img, cmap='gray', norm=LogNorm(), origin='lower')
        plt.imshow(img, cmap=plt.cm.bone, norm=LogNorm(), origin='lower')
    else:
        # plt.imshow(img, cmap='gray', origin='lower')
        plt.imshow(img, cmap=plt.cm.bone, origin='lower')
    plt.savefig(buff, dpi=42)

    buff.seek(0)
    plt.close('all')

    thumb = {
        "source_id": a["objectId"],
        "data": base64.b64encode(buff.read()).decode("utf-8"),
        "ttype": ttype,
    }

    return thumb


''' Alert filters '''


def make_triplet(alert, to_tpu: bool = False):
    """
        Feed in alert packet
    """
    cutout_dict = dict()

    for cutout in ('science', 'template', 'difference'):
        cutout_data = alert[f'cutout{cutout.capitalize()}']['stampData']

        # unzip
        with gzip.open(io.BytesIO(cutout_data), 'rb') as f:
            with fits.open(io.BytesIO(f.read())) as hdu:
                data = hdu[0].data
                # replace nans with zeros
                cutout_dict[cutout] = np.nan_to_num(data)
                # L2-normalize
                cutout_dict[cutout] /= np.linalg.norm(cutout_dict[cutout])

        # pad to 63x63 if smaller
        shape = cutout_dict[cutout].shape
        if shape != (63, 63):
            # print(f'Shape of {candid}/{cutout}: {shape}, padding to (63, 63)')
            cutout_dict[cutout] = np.pad(cutout_dict[cutout], [(0, 63 - shape[0]), (0, 63 - shape[1])],
                                         mode='constant', constant_values=1e-9)

    triplet = np.zeros((63, 63, 3))
    triplet[:, :, 0] = cutout_dict['science']
    triplet[:, :, 1] = cutout_dict['template']
    triplet[:, :, 2] = cutout_dict['difference']

    if to_tpu:
        # Edge TPUs require additional processing
        triplet = np.rint(triplet * 128 + 128).astype(np.uint8).flatten()

    return triplet


def alert_filter__ml(alert, ml_models: dict = None) -> dict:
    """
        Execute ML models
    """

    scores = dict()

    try:
        ''' braai '''
        triplet = make_triplet(alert)
        triplets = np.expand_dims(triplet, axis=0)
        braai = ml_models['braai']['model'].predict(x=triplets)[0]
        # braai = 1.0
        scores['braai'] = float(braai)
        scores['braai_version'] = ml_models['braai']['version']
    except Exception as e:
        print(time_stamp(), str(e))

    return scores


# cone search radius:
cone_search_radius = float(config['xmatch']['cone_search_radius'])
# convert to rad:
if config['xmatch']['cone_search_unit'] == 'arcsec':
    cone_search_radius *= np.pi / 180.0 / 3600.
elif config['xmatch']['cone_search_unit'] == 'arcmin':
    cone_search_radius *= np.pi / 180.0 / 60.
elif config['xmatch']['cone_search_unit'] == 'deg':
    cone_search_radius *= np.pi / 180.0
elif config['xmatch']['cone_search_unit'] == 'rad':
    cone_search_radius *= 1
else:
    raise Exception('Unknown cone search unit. Must be in [deg, rad, arcsec, arcmin]')


def alert_filter__xmatch(database, alert) -> dict:
    """
        Cross-match alerts
    """

    xmatches = dict()

    try:
        ra_geojson = float(alert['candidate']['ra'])
        # geojson-friendly ra:
        ra_geojson -= 180.0
        dec_geojson = float(alert['candidate']['dec'])

        ''' catalogs '''
        for catalog in config['xmatch']['catalogs']:
            catalog_filter = config['xmatch']['catalogs'][catalog]['filter']
            catalog_projection = config['xmatch']['catalogs'][catalog]['projection']

            object_position_query = dict()
            object_position_query['coordinates.radec_geojson'] = {
                '$geoWithin': {'$centerSphere': [[ra_geojson, dec_geojson], cone_search_radius]}}
            s = database[catalog].find({**object_position_query, **catalog_filter},
                                 {**catalog_projection})
            xmatches[catalog] = list(s)

    except Exception as e:
        print(time_stamp(), str(e))

    return xmatches


# cone search radius in deg:
cone_search_radius_clu = 3.0
# convert deg to rad:
cone_search_radius_clu *= np.pi / 180.0


def alert_filter__xmatch_clu(database, alert, size_margin=3, clu_version='CLU_20190625') -> dict:
    """
        Filter to apply to each alert: cross-match with the CLU catalog

    :param database:
    :param alert:
    :param size_margin: multiply galaxy size by this much before looking for a match
    :param clu_version: CLU catalog version
    :return:
    """

    xmatches = dict()

    try:
        ra = float(alert['candidate']['ra'])
        dec = float(alert['candidate']['dec'])

        # geojson-friendly ra:
        ra_geojson = float(alert['candidate']['ra']) - 180.0
        dec_geojson = dec

        catalog_filter = {}
        catalog_projection = {"_id": 1, "name": 1, "ra": 1, "dec": 1,
                              "a": 1, "b2a": 1, "pa": 1, "z": 1,
                              "sfr_fuv": 1, "mstar": 1, "sfr_ha": 1,
                              "coordinates.radec_str": 1}

        # first do a coarse search of everything that is around
        object_position_query = dict()
        object_position_query['coordinates.radec_geojson'] = {
            '$geoWithin': {'$centerSphere': [[ra_geojson, dec_geojson], cone_search_radius_clu]}}
        s = database[clu_version].find({**object_position_query, **catalog_filter},
                                       {**catalog_projection})
        galaxies = list(s)

        # these guys are very big, so check them separately
        M31 = {'_id': 596900, 'name': 'PGC2557',
               'ra': 10.6847, 'dec': 41.26901, 'a': 6.35156, 'b2a': 0.32, 'pa': 35.0,
               'sfr_fuv': None, 'mstar': 253816876.412914, 'sfr_ha': 0,
               'coordinates': {'radec_geojson': ["00:42:44.3503", "41:16:08.634"]}
               }
        M33 = {'_id': 597543, 'name': 'PGC5818',
               'ra': 23.46204, 'dec': 30.66022, 'a': 2.35983, 'b2a': 0.59, 'pa': 23.0,
               'sfr_fuv': None, 'mstar': 4502777.420493, 'sfr_ha': 0,
               'coordinates': {'radec_geojson': ["01:33:50.8900", "30:39:36.800"]}
               }

        # do elliptical matches
        matches = []

        for galaxy in galaxies + [M31, M33]:
            alpha1, delta01 = galaxy['ra'], galaxy['dec']
            d0, axis_ratio, PA0 = galaxy['a'], galaxy['b2a'], galaxy['pa']

            # no shape info for galaxy? replace with median values
            if d0 < -990:
                d0 = 0.0265889
            if axis_ratio < -990:
                axis_ratio = 0.61
            if PA0 < -990:
                PA0 = 86.0

            in_galaxy = in_ellipse(ra, dec, alpha1, delta01, size_margin * d0, axis_ratio, PA0)

            if in_galaxy:
                match = galaxy
                distance_arcsec = round(great_circle_distance(ra, dec, alpha1, delta01) * 3600, 2)
                match['coordinates']['distance_arcsec'] = distance_arcsec
                matches.append(match)

        xmatches[clu_version] = matches

    except Exception as e:
        print(time_stamp(), str(e))

    return xmatches


def alert_filter__user_defined(database, filter_templates, alert,
                               catalog: str = 'ZTF_alerts', max_time_ms: int = 500) -> dict:
    """
        Evaluate user defined filters
    :param database:
    :param filter_templates:
    :param alert:
    :param catalog:
    :param max_time_ms:
    :return:
    """
    passed_filters = dict()
    for filter_template in filter_templates:
        try:
            _filter = deepcopy(filter_template)
            # match candid
            _filter['pipeline'][0]["$match"]["candid"] = alert['candid']
            # todo: evaluate magic variables (such as "<jd>" or "<jd_date>") here with DFS for each pipeline stage
            #       or serialize, replace, and de-serialize
            passed_filter = list(database[catalog].aggregate(_filter['pipeline'],
                                                             allowDiskUse=False, maxTimeMS=max_time_ms))
            # passed filter? then len(passed_filter) should be = 1
            if len(passed_filter) > 0:
                print(f'{time_stamp()}: {alert["candid"]} passed filter {_filter["_id"]}')
                passed_filters[_filter['_id']] = passed_filter[0]

        except Exception as e:
            print(f'{time_stamp()}: filter {filter_template["_id"]} execution failed on alert {alert["candid"]}: {e}')
            continue

    return passed_filters


def listener(topic, bootstrap_servers='', offset_reset='earliest',
             group=None, path_alerts=None, path_tess=None, save_packets=True,
             test=False):
    """
        Listen to a topic
    :param topic:
    :param bootstrap_servers:
    :param offset_reset:
    :param group:
    :param path_alerts:
    :param path_tess:
    :param save_packets:
    :param test: when testing, terminate once reached end of partition
    :return:
    """

    # Configure consumer connection to Kafka broker
    conf = {'bootstrap.servers': bootstrap_servers,
            # 'error_cb': error_cb,
            'default.topic.config': {'auto.offset.reset': offset_reset}}
    if group is not None:
        conf['group.id'] = group
    else:
        conf['group.id'] = os.environ.get('HOSTNAME', 'kowalski')

    # make it unique:
    conf['group.id'] = f"{conf['group.id']}_{datetime.datetime.utcnow().strftime('%Y-%m-%d_%H:%M:%S.%f')}"

    # date string:
    datestr = topic.split('_')[1]

    # Start alert stream consumer
    stream_reader = AlertConsumer(topic, **conf)

    while True:
        try:
            # poll!
            stream_reader.poll(path_alerts=path_alerts, path_tess=path_tess,
                               datestr=datestr, save_packets=save_packets)

        except EopError as e:
            # Write when reaching end of partition
            print(f'{time_stamp()}: e.message')
            if test:
                # when testing, terminate once reached end of partition:
                sys.exit()
        except IndexError:
            print(time_stamp(), '%% Data cannot be decoded\n')
        except UnicodeDecodeError:
            print(time_stamp(), '%% Unexpected data format received\n')
        except KeyboardInterrupt:
            print(time_stamp(), '%% Aborted by user\n')
            sys.exit()
        except Exception as e:
            print(time_stamp(), str(e))
            _err = traceback.format_exc()
            print(time_stamp(), str(_err))
            sys.exit()


def ingester(obs_date=None, save_packets=True, test=False):
    """
        Watchdog for topic listeners

    :param obs_date:
    :param save_packets:
    :param test:
    :return:
    """

    topics_on_watch = dict()

    while True:

        try:
            # get kafka topic names with kafka-topics command
            if not test:
                # Production Kafka stream at IPAC
                kafka_cmd = [os.path.join(config['path']['path_kafka'], 'bin', 'kafka-topics.sh'),
                             '--zookeeper', config['kafka']['zookeeper'], '-list']
            else:
                # Local test stream
                kafka_cmd = [os.path.join(config['path']['path_kafka'], 'bin', 'kafka-topics.sh'),
                             '--zookeeper', config['kafka']['zookeeper.test'], '-list']
            # print(kafka_cmd)

            topics = subprocess.run(kafka_cmd, stdout=subprocess.PIPE).stdout.decode('utf-8').split('\n')[:-1]
            # print(topics)

            if obs_date is None:
                datestr = datetime.datetime.utcnow().strftime('%Y%m%d')
            else:
                datestr = obs_date
            # as of 20180403 naming convention is ztf_%Y%m%d_programidN
            # exclude ZUDS, ingest separately
            topics_tonight = [t for t in topics if (datestr in t) and ('programid' in t) and ('zuds' not in t)]
            print(f'{time_stamp()}: Topics: {topics_tonight}')

            for t in topics_tonight:
                if t not in topics_on_watch:
                    print(f'{time_stamp()}: starting listener thread for {t}')
                    offset_reset = config['kafka']['default.topic.config']['auto.offset.reset']
                    if not test:
                        bootstrap_servers = config['kafka']['bootstrap.servers']
                    else:
                        bootstrap_servers = config['kafka']['bootstrap.test.servers']
                    group = '{:s}'.format(config['kafka']['group'])
                    # print(group)
                    path_alerts = config['path']['path_alerts']
                    path_tess = config['path']['path_tess']
                    topics_on_watch[t] = multiprocessing.Process(target=listener,
                                                                 args=(t, bootstrap_servers,
                                                                       offset_reset, group,
                                                                       path_alerts, path_tess,
                                                                       save_packets, test))
                    topics_on_watch[t].daemon = True
                    topics_on_watch[t].start()

                else:
                    print(f'{time_stamp()}: performing thread health check for {t}')
                    try:
                        # if not topics_on_watch[t].isAlive():
                        if not topics_on_watch[t].is_alive():
                            print(f'{time_stamp()}: {t} died, removing')
                            # topics_on_watch[t].terminate()
                            topics_on_watch.pop(t, None)
                        else:
                            print(f'{time_stamp()}: {t} appears normal')
                    except Exception as _e:
                        print(f'{time_stamp()}: Failed to perform health check', str(_e))
                        pass

            if test:
                # print('aloha')
                time.sleep(120)
                # when testing, wait for topic listeners to pull all the data, then break
                for t in topics_on_watch:
                    topics_on_watch[t].kill()
                break

        except Exception as e:
            print(f'{time_stamp()}:  {str(e)}')
            _err = traceback.format_exc()
            print(f'{time_stamp()}: {str(_err)}')

        if obs_date is None:
            time.sleep(300)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Fetch AVRO packets from Kafka streams and ingest them into DB')
    parser.add_argument('--obsdate', help='observing date')
    parser.add_argument('--noio', help='reduce i/o - do not save packets', action='store_true')
    parser.add_argument('--test', help='listen to the test stream', action='store_true')

    args = parser.parse_args()

    ingester(obs_date=args.obsdate,
             save_packets=not args.noio,
             test=args.test)
