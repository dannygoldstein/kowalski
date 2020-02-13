# kowalski

Enhancing time-domain astronomy with the [Zwicky Transient Facility](https://ztf.caltech.edu).

## Spin up kowalski

Clone the repo and cd to the cloned directory:
```bash
git clone https://github.com/dmitryduev/kowalski-dev.git kowalski
cd kowalski
```

Create `secrets.json` with the secrets:
```json
{
  "server" : {
    "admin_username": "admin",
    "admin_password": "admin"
  },

  "database": {
    "admin_username": "mongoadmin",
    "admin_password": "mongoadminsecret",
    "username": "ztf",
    "password": "ztf"
  },

  "kafka": {
    "bootstrap.servers": "192.168.0.64:9092,192.168.0.65:9092,192.168.0.66:9092",
    "zookeeper": "192.168.0.64:2181",
    "bootstrap.test.servers": "localhost:9092",
    "zookeeper.test": "localhost:2181"
  },
  
  "skyportal": {
    "username": "kowalski",
    "password": "password"
  },

  "ztf_depot": {
    "username": "username",
    "password": "password"
  },

  "ztf_ops": {
    "url": "http://site/allexp.tbl",
    "username": "username",
    "password": "password"
  }
}

```

Run `docker-compose` to fire up `kowalski`:
```bash
docker-compose up --build -d
```

Shut down `kowalski`:
```bash
docker-compose down
```

## Run tests

Ingester:
```bash
docker exec -it kowalski_ingester_1 python -m pytest -s test_ingester.py
```

API:
```bash
docker exec -it kowalski_api_1 python -m pytest -s test_api.py
```

TODO: The first test ingests 11 (real!) test alerts. Try out a few queries:

`/api/auth`

Headers:
```json
{"Content-Type": "application/json"}
```

Body:
```json
{
    "username": "admin",
    "password": "admin"
}
```


`/api/queries`

Headers:
```json
{"Authorization": <TOKEN>, "Content-Type": "application/json"}
```

Body:
```json
{
    "query_type": "find",
    "query": {
        "catalog": "ZTF_alerts",
    	"filter": {"classifications.braai": {"$gt": 0.9}},
    	"projection": {"_id": 0, "candid": 1, "classifications.braai": 1}
    }
}
```

## Miscellaneous

Build and run a dedicated container for the Kafka producer (for testing):
```bash
docker build --rm -t kafka_producer:latest -f kafka-producer.Dockerfile .
docker run -it --rm --name kafka_producer -p 2181:2181 -p 9092:9092 kafka_producer:latest
docker exec -it kafka_producer /bin/bash
``` 