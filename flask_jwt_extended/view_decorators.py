from functools import wraps

from flask import request
from werkzeug.security import safe_str_cmp
try:
    from flask import _app_ctx_stack as ctx_stack
except ImportError:  # pragma: no cover
    from flask import _request_ctx_stack as ctx_stack

from flask_jwt_extended.config import config
from flask_jwt_extended.exceptions import (
    InvalidHeaderError, NoAuthorizationError, WrongTokenError,
    FreshTokenRequired, CSRFError, UserLoadError, RevokedTokenError,
    UserClaimsVerificationError
)
from flask_jwt_extended.tokens import decode_jwt
from flask_jwt_extended.utils import (
    has_user_loader, user_loader, token_in_blacklist,
    has_token_in_blacklist_callback, verify_token_claims
)


def jwt_required(fn):
    """
    If you decorate a view with this, it will ensure that the requester has a
    valid JWT before calling the actual view. This does not check the freshness
    of the token.

    See also: fresh_jwt_required()

    :param fn: The view function to decorate
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        jwt_data = _decode_jwt_from_request(request_type='access')
        ctx_stack.top.jwt = jwt_data
        _load_user(jwt_data[config.identity_claim])
        return fn(*args, **kwargs)
    return wrapper


def jwt_optional(fn):
    """
    If you decorate a view with this, it will check the request for a valid
    JWT and put it into the Flask application context before calling the view.
    If no authorization header is present, the view will be called without the
    application context being changed. Other authentication errors are not
    affected.

    :param fn: The view function to decorate
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            jwt_data = _decode_jwt_from_request(request_type='access')
            ctx_stack.top.jwt = jwt_data
            _load_user(jwt_data[config.identity_claim])
        except NoAuthorizationError:
            pass
        return fn(*args, **kwargs)
    return wrapper


def fresh_jwt_required(fn):
    """
    If you decorate a vew with this, it will ensure that the requester has a
    valid JWT before calling the actual view.

    See also: jwt_required()

    :param fn: The view function to decorate
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Check if the token is fresh
        jwt_data = _decode_jwt_from_request(request_type='access')
        if not jwt_data['fresh']:
            raise FreshTokenRequired('Fresh token required')

        ctx_stack.top.jwt = jwt_data
        _load_user(jwt_data[config.identity_claim])
        return fn(*args, **kwargs)
    return wrapper


def jwt_refresh_token_required(fn):
    """
    If you decorate a view with this, it will insure that the requester has a
    valid JWT refresh token before calling the actual view. If the token is
    invalid, expired, not present, etc, the appropriate callback will be called
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        jwt_data = _decode_jwt_from_request(request_type='refresh')
        ctx_stack.top.jwt = jwt_data
        _load_user(jwt_data[config.identity_claim])
        return fn(*args, **kwargs)
    return wrapper


def _load_user(identity):
    if has_user_loader():
        user = user_loader(identity)
        if user is None:
            raise UserLoadError("user_loader returned None for {}".format(identity))
        else:
            ctx_stack.top.jwt_user = user


def _token_blacklisted(decoded_token, request_type):
    if not config.blacklist_enabled:
        return False
    if not has_token_in_blacklist_callback():
        raise RuntimeError("A token_in_blacklist_callback must be provided via "
                           "the '@token_in_blacklist_loader' if "
                           "JWT_BLACKLIST_ENABLED is True")

    if config.blacklist_access_tokens and request_type == 'access':
        return token_in_blacklist(decoded_token)
    if config.blacklist_refresh_tokens and request_type == 'refresh':
        return token_in_blacklist(decoded_token)
    return False


def _decode_jwt_from_headers():
    header_name = config.header_name
    header_type = config.header_type

    # Verify we have the auth header
    jwt_header = request.headers.get(header_name, None)
    if not jwt_header:
        raise NoAuthorizationError("Missing {} Header".format(header_name))

    # Make sure the header is in a valid format that we are expecting, ie
    # <HeaderName>: <HeaderType(optional)> <JWT>
    parts = jwt_header.split()
    if not header_type:
        if len(parts) != 1:
            msg = "Bad {} header. Expected value '<JWT>'".format(header_name)
            raise InvalidHeaderError(msg)
        token = parts[0]
    else:
        if parts[0] != header_type or len(parts) != 2:
            msg = "Bad {} header. Expected value '{} <JWT>'".format(header_name, header_type)
            raise InvalidHeaderError(msg)
        token = parts[1]

    return decode_jwt(
        encoded_token=token,
        secret=config.decode_key,
        algorithm=config.algorithm,
        csrf=False,
        identity_claim=config.identity_claim
    )


def _decode_jwt_from_cookies(request_type):
    if request_type == 'access':
        cookie_key = config.access_cookie_name
        csrf_header_key = config.access_csrf_header_name
    else:
        cookie_key = config.refresh_cookie_name
        csrf_header_key = config.refresh_csrf_header_name

    encoded_token = request.cookies.get(cookie_key)
    if not encoded_token:
        raise NoAuthorizationError('Missing cookie "{}"'.format(cookie_key))

    decoded_token = decode_jwt(
        encoded_token=encoded_token,
        secret=config.decode_key,
        algorithm=config.algorithm,
        csrf=config.csrf_protect,
        identity_claim=config.identity_claim
    )

    # Verify csrf double submit tokens match if required
    if config.csrf_protect and request.method in config.csrf_request_methods:
        csrf_token_in_token = decoded_token['csrf']
        csrf_token_in_header = request.headers.get(csrf_header_key, None)

        if not csrf_token_in_header:
            raise CSRFError("Missing CSRF token in headers")
        if not safe_str_cmp(csrf_token_in_header, csrf_token_in_token):
            raise CSRFError("CSRF double submit tokens do not match")

    return decoded_token


def _decode_jwt_from_request(request_type):
    # We have three cases here, having jwts in both cookies and headers is
    # valid, or the jwt can only be saved in one of cookies or headers. Check
    # all cases here.
    if config.jwt_in_cookies and config.jwt_in_headers:
        try:
            decoded_token = _decode_jwt_from_cookies(request_type)
        except NoAuthorizationError:
            try:
                decoded_token = _decode_jwt_from_headers()
            except NoAuthorizationError:
                raise NoAuthorizationError("Missing JWT in headers and cookies")
    elif config.jwt_in_headers:
        decoded_token = _decode_jwt_from_headers()
    else:
        decoded_token = _decode_jwt_from_cookies(request_type)

    # Make sure the type of token we received matches the request type we expect
    if decoded_token['type'] != request_type:
        raise WrongTokenError('Only {} tokens can access this endpoint'.format(request_type))

    # Check if the custom claims in access tokens are valid
    if request_type == 'access':
        if not verify_token_claims(decoded_token['user_claims']):
            raise UserClaimsVerificationError('user_claims verification failed')

    # If blacklisting is enabled, see if this token has been revoked
    if _token_blacklisted(decoded_token, request_type):
        raise RevokedTokenError('Token has been revoked')

    return decoded_token
