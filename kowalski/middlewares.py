from aiohttp import web
from copy import deepcopy
from functools import wraps
import jwt


@web.middleware
async def auth_middleware(request, handler):
    """
        auth middleware
    :param request:
    :param handler:
    :return:
    """
    request.user = None
    jwt_token = request.headers.get('authorization', None)

    if jwt_token:
        try:
            # accept both "Authorization: Bearer <token>" and "Authorization: <token>" headers
            if 'bearer' in deepcopy(jwt_token).lower():
                jwt_token = jwt_token.split()[1]

            payload = jwt.decode(jwt_token, request.app['JWT']['JWT_SECRET'],
                                 algorithms=[request.app['JWT']['JWT_ALGORITHM']])
        except (jwt.DecodeError, jwt.ExpiredSignatureError):
            return web.json_response({'status': 'error', 'message': 'token is invalid'}, status=400)

        request.user = payload['user_id']

    response = await handler(request)

    return response


def auth_required(func):
    """
        Decorator to ensure successful user authorization to use the API
    :param func:
    :return:
    """
    def wrapper(request):
        if not request.user:
            return web.json_response({'status': 'error', 'message': 'auth required'}, status=401)
        return func(request)
    return wrapper


def admin_required(admin: str = 'admin'):
    """
        Decorator to ensure user authorization _and_ admin rights
    :param admin: admin name
    :return:
    """
    def inner_function(func):
        def wrapper(request):
            if not request.user:
                return web.json_response({'status': 'error', 'message': 'auth required'}, status=401)
            if request.user != admin:
                return web.json_response({'status': 'error', 'message': 'admin rights required'}, status=403)
            return func(request)
        return wrapper

    return inner_function
