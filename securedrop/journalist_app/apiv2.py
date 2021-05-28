import collections.abc
import json

from datetime import datetime, timedelta
from typing import Tuple, Callable, Any, Set, Union

import flask
import werkzeug
from flask import abort, Blueprint, current_app, jsonify, request
from functools import wraps

from sqlalchemy import Column
from sqlalchemy.exc import IntegrityError
import os
from os import path
from uuid import UUID
from werkzeug.exceptions import default_exceptions

from db import db
from journalist_app import utils
from models import (Group, GroupMember, Journalist, Reply, SeenReply, Source, Submission, JournalistReply,
                    LoginThrottledException, InvalidUsernameException,
                    BadTokenException, WrongPasswordException, SourceMessage, active_securedrop_groups)
from sdconfig import config, SDConfig
import shared_api
from store import NotEncrypted

from signal_groups.api.auth import AuthCredentialPresentation
from signal_groups.api.groups import GroupPublicParams, UuidCiphertext
from signal_protocol.curve import PublicKey
from signal_protocol.sealed_sender import SenderCertificate

"""
Reply: From journalist to source
Message: From source to journalist
File: Encrypted blob encrypted symmetrically and stored in blob store. NOT IMPLEMENTED.

BEHAVIOR CHANGE:
* Messages and replies encrypted per-recipient and are deleted after download (once confirmed by client).

ENDPOINTS:
* Signed prekey refresh endpoint
* OT Prekey refresh endpoint
* TK: blob store
* TK: admin
"""

TOKEN_EXPIRATION_MINS = 60 * 8

def _authenticate_user_from_auth_header(request: flask.Request) -> Journalist:
    try:
        auth_header = request.headers['Authorization']
    except KeyError:
        return abort(403, 'API token not found in Authorization header.')

    if auth_header:
        split = auth_header.split(" ")
        if len(split) != 2 or split[0] != 'Token':
            abort(403, 'Malformed authorization header.')
        auth_token = split[1]
    else:
        auth_token = ''
    authenticated_user = Journalist.validate_api_token_and_get_user(auth_token)
    if not authenticated_user:
        return abort(403, 'API token is invalid or expired.')
    return authenticated_user


def token_required(f: Callable) -> Callable:
    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        _authenticate_user_from_auth_header(request)
        return f(*args, **kwargs)
    return decorated_function


def get_or_404(model: db.Model, object_id: str, column: Column) -> db.Model:
    result = model.query.filter(column == object_id).one_or_none()
    if result is None:
        abort(404)
    return result


def make_blueprint(config: SDConfig) -> Blueprint:
    api = Blueprint('apiv2', __name__)

    @api.route('/')
    def get_endpoints() -> Tuple[flask.Response, int]:
        endpoints = {'sources_url': '/api/v2/sources',
                     'current_user_url': '/api/v2/user',
                     'all_users_url': '/api/v2/users',
                     'submissions_url': '/api/v2/submissions',
                     'replies_url': '/api/v2/replies',
                     'seen_url': '/api/v2/seen',
                     'auth_token_url': '/api/v2/token',
                     'signal_registration_url': '/api/v2/register'}
        return jsonify(endpoints), 200

    # Before every post, we validate the payload before processing the request
    @api.before_request
    def validate_data() -> None:
        if request.method == 'POST':
            if not request.data:
                dataless_endpoints = [
                    'add_star',
                    'remove_star',
                    'logout',
                    'confirm_message'
                ]
                for endpoint in dataless_endpoints:
                    if request.endpoint == 'apiv2.' + endpoint:
                        return
                abort(400, 'malformed request')
            # other requests must have valid JSON payload
            else:
                try:
                    json.loads(request.data.decode('utf-8'))
                except (ValueError):
                    abort(400, 'malformed request')

    @api.route('/token', methods=['POST'])
    def get_token() -> Tuple[flask.Response, int]:
        creds = json.loads(request.data.decode('utf-8'))

        username = creds.get('username', None)
        passphrase = creds.get('passphrase', None)
        one_time_code = creds.get('one_time_code', None)

        if username is None:
            abort(400, 'username field is missing')
        if passphrase is None:
            abort(400, 'passphrase field is missing')
        if one_time_code is None:
            abort(400, 'one_time_code field is missing')

        try:
            journalist = Journalist.login(username, passphrase, one_time_code)
            token_expiry = datetime.utcnow() + timedelta(
                seconds=TOKEN_EXPIRATION_MINS * 60)

            response = jsonify({
                'token': journalist.generate_api_token(expiration=TOKEN_EXPIRATION_MINS * 60),
                'expiration': token_expiry.isoformat() + 'Z',
                'journalist_uuid': journalist.uuid,
                'journalist_first_name': journalist.first_name,
                'journalist_last_name': journalist.last_name,
            })

            # Update access metadata
            journalist.last_access = datetime.utcnow()
            db.session.add(journalist)
            db.session.commit()

            return response, 200
        except (LoginThrottledException, InvalidUsernameException,
                BadTokenException, WrongPasswordException):
            return abort(403, 'Token authentication failed.')

    @api.route('/register', methods=['POST'])
    @token_required
    def signal_registration() -> Tuple[flask.Response, int]:
        """
        One must first authenticate using their user account prior to being able to
        register their Signal deets. One should provide in the JSON body of the request
        identity_key, signed_prekey, signed_prekey_timestamp, prekey_signature, registration_id,
        along with a list of one-time prekeys.

        TODO: Should this entire payload also be signed? (Probably)
        Investigate how Java signal server handles this.
        """
        user = _authenticate_user_from_auth_header(request)

        # TODO: Think about how best to handle users re-registering.
        if user.is_signal_registered():
            abort(400, 'your account is already registered')

        creds = json.loads(request.data.decode('utf-8'))
        identity_key = creds.get('identity_key', None)
        signed_prekey = creds.get('signed_prekey', None)
        signed_prekey_timestamp = creds.get('signed_prekey_timestamp', None)
        prekey_signature = creds.get('prekey_signature', None)
        registration_id = creds.get('registration_id', None)
        signed_prekey_id = creds.get('signed_prekey_id', None)
        # TODO: handle OT prekeys

        if identity_key is None:
            abort(400, 'identity_key field is missing')
        if signed_prekey is None:
            abort(400, 'signed_prekey field is missing')
        if signed_prekey_timestamp is None:
            abort(400, 'signed_prekey_timestamp field is missing')
        if signed_prekey_id is None:
            abort(400, 'signed_prekey_id field is missing')
        if prekey_signature is None:
            abort(400, 'prekey_signature field is missing')
        if registration_id is None:
            abort(400, 'registration_id field is missing')

        # TODO: Server _could_ also verify the prekey sig and reject.
        # Clients will be doing this too on their end, but we could also do here.

        # This enables enumeration of in-use registration ids. This is fine since these are public.
        duplicate_reg_id = Journalist.query.filter_by(registration_id=registration_id).all()
        if duplicate_reg_id:
            abort(400, 'registration_id is in use')

        if user.signed_prekey_timestamp and user.signed_prekey_timestamp > signed_prekey_timestamp:
            abort(400, 'signed prekey timestamp should be fresher than what is on the server')

        user.identity_key = identity_key
        user.signed_prekey = signed_prekey
        user.signed_prekey_timestamp = signed_prekey_timestamp
        user.prekey_signature = prekey_signature
        user.registration_id = registration_id
        user.signed_prekey_id = signed_prekey_id

        response = jsonify({
            'message': 'your account is now registered for messaging'
        })
        # TODO
        # At this point there should be some group inspection logic:
        # if not in group => "Please talk to your admin to get onboarded into a group"
        # if in group => "You can now receive messages"

        db.session.add(user)
        db.session.commit()

        return response, 200

    @api.route('/auth_credential', methods=['GET'])
    @token_required
    def auth_credential() -> Tuple[flask.Response, int]:
        user = _authenticate_user_from_auth_header(request)
        return shared_api.auth_credential(user)

    @api.route('/server_params', methods=['GET'])
    @token_required
    def server_public_params() -> Tuple[flask.Response, int]:
        return shared_api.server_public_params()

    @api.route('/sender_cert', methods=['GET'])
    @token_required
    def sender_cert() -> Tuple[flask.Response, int]:
        user = _authenticate_user_from_auth_header(request)
        return shared_api.sender_cert(user)

    @api.route('/sources/<source_uuid>/prekey_bundle', methods=['GET'])
    @token_required
    def prekey_bundle(source_uuid: str) -> Tuple[flask.Response, int]:
        """
        Get a prekey bundle to start a new session with source_uuid.
        """
        source = get_or_404(Source, source_uuid, column=Source.uuid)

        # TODO: Add one-time prekeys
        response = jsonify({
            'source_uuid': source.uuid,
            'identity_key': source.identity_key.hex(),
            'signed_prekey': source.signed_prekey.hex(),
            'signed_prekey_timestamp': source.signed_prekey_timestamp,
            'prekey_signature': source.prekey_signature.hex(),
            'registration_id': source.registration_id,
        })
        return response, 200

    @api.route('/messages', methods=['GET'])
    @token_required
    def get_message() -> Tuple[flask.Response, int]:
        """
        Get messages from sources.

        This endpoint is generic such that journalist-to-journalist
        messages can be added later.

        TODO: Pagination
        """
        user = _authenticate_user_from_auth_header(request)
        source_message = SourceMessage.query.filter_by(journalist_id=user.id).first()
        if source_message:
            return jsonify({
                "message_uuid": source_message.uuid,
                "message": source_message.message.hex(),
            }), 200
        else:
            return jsonify({"resp": "no messages"}), 200

    @api.route('/groups', methods=['GET'])
    @token_required
    def groups() -> Tuple[flask.Response, int]:
        """
        Get SecureDrop group membership information. This lets a source know
        who is onboarded to each organization.
        """

        response = jsonify(active_securedrop_groups())
        return response, 200

    # This intentionally does not have @token_required, in the body of the
    # request, the user must present a valid AuthCredentialPresentation
    @api.route('/groups/members', methods=['POST'])
    def get_group_members() -> Tuple[flask.Response, int]:
        group = shared_api.auth_as_group_member(request)
        if group:
            members = GroupMember.query.filter_by(group_id=group.id).all()
            uuid_ciphertexts = [x.uid_ciphertext.hex() for x in members]
            response = jsonify({'members': uuid_ciphertexts})
            return response, 200
        else:
            abort(403, 'err: could not authenticate')

    # This intentionally does not have @token_required, in the body of the
    # request, the user must present a valid AuthCredentialPresentation
    @api.route('/groups/members/new', methods=['POST'])
    def add_group_member() -> Tuple[flask.Response, int]:
        """
        Add member to existing group
        """
        group = shared_api.auth_as_group_member(request)
        # check for is_admin?
        if group:
            # 2. (client) Encrypt new user’s UID to create UidCiphertext using GroupSecretParams.
            # UidCiphertext = newuseruid.encrypt

            # 3. Send to server with the desired role (is_admin or not).

            # maybe?            # response = jsonify({'role': some_role})
            # return response, 200

            # 4. Server verifies the authenticated user’s Role
            # (selected using the UidCiphertext from the provided AuthCredentialPresentation) lets them add users.

            # 5. Server checks the new UidCiphertext is not already in the group.
            # If it is, and it changes the role, we make that change.
            # Else, the server adds the user to the group.

            
        else:
            abort(403, 'err: could not authenticate')

    # This intentionally does not have @token_required, in the body of the
    # request, the user must present a valid AuthCredentialPresentation
    @api.route('/groups/new', methods=['POST'])
    def create_group() -> Tuple[flask.Response, int]:
        """
        Create group
        """
        if request.json is None:
            abort(400, 'please send requests in valid JSON')

        req = json.loads(request.data.decode('utf-8'))
        raw_auth_credential_presentation = req.get('auth_credential_presentation', None)
        group_id = req.get('group_id', None)
        raw_group_public_params = req.get('group_public_params', None)
        group_members = req.get('group_members', [])
        group_admins = req.get('group_admins', [])

        if raw_auth_credential_presentation is None:
            abort(400, 'auth_credential_presentation not found in request body')
        if group_id is None:
            abort(400, 'group_id field is missing')
        if raw_group_public_params is None:
            abort(400, 'group_public_params field is missing')
        if not group_members:
            abort(400, 'group_members field is missing')
        if not group_admins:
            abort(400, 'group_admins field is missing')

        try:
            group_public_params = GroupPublicParams.deserialize(bytes.fromhex(raw_group_public_params))
        except ValueError:
            abort(400, 'err: could not deserialize GroupPublicParams')

        try:
            auth_credential_presentation = AuthCredentialPresentation.deserialize(bytes.fromhex(raw_auth_credential_presentation))
        except ValueError:
            abort(400, 'err: could not deserialize AuthCredentialPresentation')

        # Before doing anything, we must verify the auth credential presentation.
        try:
            config.server_secret_params.verify_auth_credential_presentation(group_public_params, auth_credential_presentation)
        except ZkGroupException:
            abort(403, 'err: invalid AuthCredentialPresentation')

        group_creator_uuid_ciphertext = auth_credential_presentation.get_uuid_ciphertext()
        new_group_id = bytes(group_public_params.get_group_identifier())

        # We ensure the group_id is not already in use on the server.
        group = Group.query.filter_by(group_uid=new_group_id).first()
        if group:
            abort(400, 'err: group already exists')
        # This means that users can enumerate group_ids.
        # But, they can't do anything with that info unless they are the admin.
        new_group = Group(new_group_id, group_public_params.serialize())

        members_uuid_cipher_to_add = []
        admins_uuid_cipher_to_add = []

        for entry in group_members:
            members_uuid_cipher_to_add.append(bytes.fromhex(entry))

        for entry in group_admins:
            admins_uuid_cipher_to_add.append(bytes.fromhex(entry))

        # We check the Uidciphertext is making a group including themselves.
        # TODO: allow a third role "Admin" that create groups for Journalists and Sources (i.e.
        # without themselves in it)?
        if group_creator_uuid_ciphertext.serialize().hex() not in group_members + group_admins:
            abort(400, 'err: cannot create a group for others')

        # -- PERMISSIONS FOR GROUP CREATORS --
        # For journalists, they must be an admin.
        if group_creator_uuid_ciphertext.serialize().hex() not in group_admins:
            abort(400, 'err: journalist group creators should be admins')

        # -- PERMISSIONS FOR OTHER GROUP MEMBERS --
        # Server does not know which user is the source, so, we cannot assert anything
        # about is_admin for the other group members (other than maybe there are N-1 admins).

        # Now that we've validated everything. Let's add the new objects to the database.
        db.session.add(new_group)
        db.session.commit()
        for member in members_uuid_cipher_to_add:
            db.session.add(GroupMember(new_group, member, is_admin=False))
        for admin in admins_uuid_cipher_to_add:
            db.session.add(GroupMember(new_group, admin, is_admin=True))
        db.session.commit()

        return jsonify({'message': 'Your group has been created'}), 200


    @api.route('/sources/<source_uuid>/messages', methods=['POST'])
    @token_required
    def send_message(source_uuid: str) -> Tuple[flask.Response, int]:
        """
        Send a message to source_uuid.
        """
        if request.json is None:
                abort(400, 'please send requests in valid JSON')

        if 'message' not in request.json:
                abort(400, 'message not found in request body')

        user = _authenticate_user_from_auth_header(request)
        source = get_or_404(Source, source_uuid, column=Source.uuid)

        data = request.json
        if not data['message']:
            abort(400, 'message should not be empty')

        message = JournalistReply(source, data['message'])
        db.session.add(message)
        db.session.commit()
        return jsonify({'message': 'Your message has been stored'}), 200

    @api.route('/messages/confirmation/<message_uuid>', methods=['POST'])
    @token_required
    def confirm_message(message_uuid: str) -> Tuple[flask.Response, int]:
        """
        Confirm receipt of message_uuid.
        """
        user = _authenticate_user_from_auth_header(request)
        source_message = SourceMessage.query.filter_by(uuid=message_uuid).first()

        # TODO: the timing of response reveals which of these two scenarios is happening
        # Journalist trying to delete another's message
        if source_message.journalist_id != user.id:
            abort(404, 'message not found with this (journalist uuid, message uuid) pair')

        if not source_message:  # Legit 404 (could replace with get_or_404, but wanted to customize message)
            abort(404, 'message not found with this (journalist uuid, message uuid) pair')

        # Message confirmed received by client. Remove from server.
        db.session.delete(source_message)
        db.session.commit()
        return jsonify({'message': 'Confirmed receipt of message'}), 200

    @api.route('/sources', methods=['GET'])
    @token_required
    def get_all_sources() -> Tuple[flask.Response, int]:
        sources = Source.query.filter_by(pending=False, deleted_at=None).all()
        return jsonify(
            {'sources': [source.to_json() for source in sources]}), 200

    @api.route('/sources/<source_uuid>', methods=['GET', 'DELETE'])
    @token_required
    def single_source(source_uuid: str) -> Tuple[flask.Response, int]:
        if request.method == 'GET':
            source = get_or_404(Source, source_uuid, column=Source.uuid)
            return jsonify(source.to_json()), 200
        elif request.method == 'DELETE':
            # BEHAVIOR CHANGE: This now deletes source accounts.
            source = get_or_404(Source, source_uuid, column=Source.uuid)
            utils.delete_collection(source.filesystem_id)
            return jsonify({'message': 'Source account deleted'}), 200
        else:
            abort(405)

    @api.route('/user', methods=['GET'])
    @token_required
    def get_current_user() -> Tuple[flask.Response, int]:
        user = _authenticate_user_from_auth_header(request)
        return jsonify(user.to_json()), 200

    @api.route('/users', methods=['GET'])
    @token_required
    def get_all_users() -> Tuple[flask.Response, int]:
        users = Journalist.query.all()
        return jsonify(
            {'users': [user.to_json(all_info=False) for user in users]}), 200

    @api.route('/logout', methods=['POST'])
    @token_required
    def logout() -> Tuple[flask.Response, int]:
        user = _authenticate_user_from_auth_header(request)
        auth_token = request.headers['Authorization'].split(" ")[1]
        utils.revoke_token(user, auth_token)
        return jsonify({'message': 'Your token has been revoked.'}), 200

    def _handle_api_http_exception(
        error: werkzeug.exceptions.HTTPException
    ) -> Tuple[flask.Response, int]:
        # Workaround for no blueprint-level 404/5 error handlers, see:
        # https://github.com/pallets/flask/issues/503#issuecomment-71383286
        response = jsonify({'error': error.name,
                           'message': error.description})

        return response, error.code  # type: ignore

    for code in default_exceptions:
        api.errorhandler(code)(_handle_api_http_exception)

    return api
