import math
import re
import json
import logging
import secrets
import time
from pathlib import Path
import hashlib
from Crypto.Cipher import AES
from Crypto.Util import Counter
import os
import random
import binascii
import tempfile
import shutil

import requests

from .errors import ValidationError, RequestError
from .crypto import (
    a32_to_base64, encrypt_key, base64_url_encode, encrypt_attr,
    base64_to_a32, base64_url_decode, decrypt_attr, a32_to_str,
    get_chunks, str_to_a32, decrypt_key, mpi_to_int, stringhash,
    prepare_key, make_id, makebyte, modular_inverse,
)

logger = logging.getLogger(__name__)


class Mega:
    def __init__(self, options=None):
        self.schema = 'https'
        self.domain = 'mega.co.nz'
        self.timeout = 160
        self.sid = None
        self.sequence_num = random.randint(0, 0xFFFFFFFF)
        self.request_id = make_id(10)
        self._trash_folder_node_id = None
        self.options = options or {}

    # ═══════════════════════════════════════════════════════════════
    # Login
    # ═══════════════════════════════════════════════════════════════
    def login(self, email=None, password=None):
        if email:
            self._login_user(email, password)
        else:
            self.login_anonymous()
        self._trash_folder_node_id = self.get_node_by_type(4)[0]
        logger.info('Login complete')
        return self

    def _login_user(self, email, password):
        logger.info('Logging in user...')
        email = email.lower()
        get_user_salt_resp = self._api_request({'a': 'us0', 'user': email})
        try:
            user_salt = base64_to_a32(get_user_salt_resp['s'])
        except KeyError:
            # v1 user
            password_aes = prepare_key(str_to_a32(password))
            user_hash = stringhash(email, password_aes)
        else:
            # v2 user
            pbkdf2_key = hashlib.pbkdf2_hmac(
                hash_name='sha512',
                password=password.encode(),
                salt=a32_to_str(user_salt),
                iterations=100000,
                dklen=32,
            )
            password_aes = str_to_a32(pbkdf2_key[:16])
            user_hash = base64_url_encode(pbkdf2_key[-16:])
        resp = self._api_request({'a': 'us', 'user': email, 'uh': user_hash})
        if isinstance(resp, int):
            raise RequestError(resp)
        self._login_process(resp, password_aes)

    def login_anonymous(self):
        logger.info('Logging in anonymous temporary user...')
        master_key = [random.randint(0, 0xFFFFFFFF)] * 4
        password_key = [random.randint(0, 0xFFFFFFFF)] * 4
        session_self_challenge = [random.randint(0, 0xFFFFFFFF)] * 4

        user = self._api_request({
            'a': 'up',
            'k': a32_to_base64(encrypt_key(master_key, password_key)),
            'ts': base64_url_encode(
                a32_to_str(session_self_challenge) +
                a32_to_str(encrypt_key(session_self_challenge, master_key))
            ),
        })

        resp = self._api_request({'a': 'us', 'user': user})
        if isinstance(resp, int):
            raise RequestError(resp)
        self._login_process(resp, password_key)

    def _login_process(self, resp, password):
        encrypted_master_key = base64_to_a32(resp['k'])
        self.master_key = decrypt_key(encrypted_master_key, password)

        if 'tsid' in resp:
            tsid = base64_url_decode(resp['tsid'])
            key_encrypted = a32_to_str(
                encrypt_key(str_to_a32(tsid[:16]), self.master_key))
            if key_encrypted == tsid[-16:]:
                self.sid = resp['tsid']
        elif 'csid' in resp:
            encrypted_rsa_private_key = base64_to_a32(resp['privk'])
            rsa_private_key = decrypt_key(encrypted_rsa_private_key,
                                          self.master_key)

            private_key = a32_to_str(rsa_private_key)
            rsa_private_key = [0, 0, 0, 0]
            for i in range(4):
                bitlength = (private_key[0] * 256) + private_key[1]
                bytelength = math.ceil(bitlength / 8) + 2
                rsa_private_key[i] = mpi_to_int(private_key[:bytelength])
                private_key = private_key[bytelength:]

            first_factor_p = rsa_private_key[0]
            second_factor_q = rsa_private_key[1]
            private_exponent_d = rsa_private_key[2]
            rsa_modulus_n = first_factor_p * second_factor_q

            encrypted_sid = mpi_to_int(base64_url_decode(resp['csid']))

            # Pure-Python RSA decrypt (no pycryptodome RSA private API)
            sid_int = pow(encrypted_sid, private_exponent_d, rsa_modulus_n)
            sid = '%x' % sid_int
            sid = binascii.unhexlify('0' + sid if len(sid) % 2 else sid)
            self.sid = base64_url_encode(sid[:43])

    # ═══════════════════════════════════════════════════════════════
    # API request — built-in exponential backoff (no tenacity)
    # ═══════════════════════════════════════════════════════════════
    def _api_request(self, data, max_retries=5, base_delay=2, max_delay=60):
        delay = base_delay
        last_exc = None
        for attempt in range(max_retries):
            try:
                return self._api_request_once(data)
            except RuntimeError as exc:
                last_exc = exc
                if attempt == max_retries - 1:
                    break
                logger.info(
                    'Request failed, retrying in %ss (attempt %d/%d)',
                    delay, attempt + 1, max_retries)
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
        raise last_exc

    def _api_request_once(self, data):
        params = {'id': self.sequence_num}
        self.sequence_num += 1

        if self.sid:
            params.update({'sid': self.sid})

        if not isinstance(data, list):
            data = [data]

        url = f'{self.schema}://g.api.{self.domain}/cs'
        response = requests.post(
            url,
            params=params,
            data=json.dumps(data),
            timeout=self.timeout,
        )
        json_resp = json.loads(response.text)

        int_resp = None
        if isinstance(json_resp, list):
            if json_resp and isinstance(json_resp[0], int):
                int_resp = json_resp[0]
        elif isinstance(json_resp, int):
            int_resp = json_resp

        if int_resp is not None:
            if int_resp == 0:
                return int_resp
            if int_resp == -3:
                raise RuntimeError('Request failed, retrying')
            raise RequestError(int_resp)

        return json_resp[0]

    # ═══════════════════════════════════════════════════════════════
    # URL parsing
    # ═══════════════════════════════════════════════════════════════
    def _parse_url(self, url):
        """Parse file id and key from url."""
        if '/file/' in url:
            url = url.replace(' ', '')
            file_id = re.findall(r'\W\w\w\w\w\w\w\w\w\W', url)[0][1:-1]
            id_index = re.search(file_id, url).end()
            key = url[id_index + 1:]
            return f'{file_id}!{key}'
        elif '!' in url:
            match = re.findall(r'/#!(.*)', url)
            return match[0]
        else:
            raise RequestError('Url key missing')

    # ═══════════════════════════════════════════════════════════════
    # File tree processing
    # ═══════════════════════════════════════════════════════════════
    def _process_file(self, file, shared_keys):
        if file['t'] in (0, 1):
            keys = dict(
                keypart.split(':', 1) for keypart in file['k'].split('/')
                if ':' in keypart)
            uid = file['u']
            key = None
            # my objects
            if uid in keys:
                key = decrypt_key(base64_to_a32(keys[uid]), self.master_key)
            # shared folders
            elif 'su' in file and 'sk' in file and ':' in file['k']:
                shared_key = decrypt_key(base64_to_a32(file['sk']),
                                         self.master_key)
                key = decrypt_key(base64_to_a32(keys[file['h']]), shared_key)
                if file['su'] not in shared_keys:
                    shared_keys[file['su']] = {}
                shared_keys[file['su']][file['h']] = shared_key
            # shared files
            elif file['u'] and file['u'] in shared_keys:
                for hkey in shared_keys[file['u']]:
                    shared_key = shared_keys[file['u']][hkey]
                    if hkey in keys:
                        key = keys[hkey]
                        key = decrypt_key(base64_to_a32(key), shared_key)
                        break
            if file['h'] and file['h'] in shared_keys.get('EXP', ()):
                shared_key = shared_keys['EXP'][file['h']]
                encrypted_key = str_to_a32(
                    base64_url_decode(file['k'].split(':')[-1]))
                key = decrypt_key(encrypted_key, shared_key)
                file['shared_folder_key'] = shared_key
            if key is not None:
                # file
                if file['t'] == 0:
                    k = (key[0] ^ key[4], key[1] ^ key[5], key[2] ^ key[6],
                         key[3] ^ key[7])
                    file['iv'] = key[4:6] + (0, 0)
                    file['meta_mac'] = key[6:8]
                # folder
                else:
                    k = key
                file['key'] = key
                file['k'] = k
                attributes = base64_url_decode(file['a'])
                attributes = decrypt_attr(attributes, k)
                file['a'] = attributes
            # other => wrong object
            elif file['k'] == '':
                file['a'] = False
        elif file['t'] == 2:
            self.root_id = file['h']
            file['a'] = {'n': 'Cloud Drive'}
        elif file['t'] == 3:
            self.inbox_id = file['h']
            file['a'] = {'n': 'Inbox'}
        elif file['t'] == 4:
            self.trashbin_id = file['h']
            file['a'] = {'n': 'Rubbish Bin'}
        return file

    def _init_shared_keys(self, files, shared_keys):
        """
        Init shared key not associated with a user.
        Keys are stored in files['s'] and files['ok'].
        """
        ok_dict = {}
        for ok_item in files.get('ok', []):
            shared_key = decrypt_key(base64_to_a32(ok_item['k']),
                                     self.master_key)
            ok_dict[ok_item['h']] = shared_key
        for s_item in files.get('s', []):
            if s_item['u'] not in shared_keys:
                shared_keys[s_item['u']] = {}
            if s_item['h'] in ok_dict:
                shared_keys[s_item['u']][s_item['h']] = ok_dict[s_item['h']]
        self.shared_keys = shared_keys

    # ═══════════════════════════════════════════════════════════════
    # Path resolution and file lookup
    # ═══════════════════════════════════════════════════════════════
    def find_path_descriptor(self, path, files=None):
        """
        Find descriptor of folder inside a path.
        e.g.: folder1/folder2/folder3
        """
        paths = path.split('/')
        files = files or self.get_files()
        parent_desc = self.root_id
        found = False
        for foldername in paths:
            if foldername != '':
                for file in files.items():
                    if (file[1]['a'] and file[1]['t']
                            and file[1]['a']['n'] == foldername):
                        if parent_desc == file[1]['p']:
                            parent_desc = file[0]
                            found = True
                if found:
                    found = False
                else:
                    return None
        return parent_desc

    def find(self, filename=None, handle=None, exclude_deleted=False):
        """Return file object from given filename."""
        files = self.get_files()
        if handle:
            return files[handle]
        path = Path(filename)
        filename = path.name
        parent_dir_name = path.parent.name
        for file in list(files.items()):
            parent_node_id = None
            try:
                if parent_dir_name:
                    parent_node_id = self.find_path_descriptor(
                        parent_dir_name, files=files)
                    if (filename and parent_node_id and file[1]['a']
                            and file[1]['a']['n'] == filename
                            and parent_node_id == file[1]['p']):
                        if (exclude_deleted and self._trash_folder_node_id
                                == file[1]['p']):
                            continue
                        return file
                elif (filename and file[1]['a']
                      and file[1]['a']['n'] == filename):
                    if (exclude_deleted
                            and self._trash_folder_node_id == file[1]['p']):
                        continue
                    return file
            except TypeError:
                continue

    def get_files(self):
        logger.info('Getting all files...')
        files = self._api_request({'a': 'f', 'c': 1, 'r': 1})
        files_dict = {}
        shared_keys = {}
        self._init_shared_keys(files, shared_keys)
        for file in files['f']:
            processed_file = self._process_file(file, shared_keys)
            if processed_file['a']:
                files_dict[file['h']] = processed_file
        return files_dict

    def get_node_by_type(self, type):
        """
        Get a node by its numeric type id:
        0: file, 1: dir, 2: root, 3: inbox, 4: trash
        """
        nodes = self.get_files()
        for node in list(nodes.items()):
            if node[1]['t'] == type:
                return node

    def get_files_in_node(self, target):
        """Get all files in a given target, e.g. 4=trash."""
        if type(target) == int:
            node_id = self.get_node_by_type(target)
        else:
            node_id = [target]

        files = self._api_request({'a': 'f', 'c': 1})
        files_dict = {}
        shared_keys = {}
        self._init_shared_keys(files, shared_keys)
        for file in files['f']:
            processed_file = self._process_file(file, shared_keys)
            if processed_file['a'] and processed_file['p'] == node_id[0]:
                files_dict[file['h']] = processed_file
        return files_dict

    def get_id_from_public_handle(self, public_handle):
        node_data = self._api_request({'a': 'f', 'f': 1, 'p': public_handle})
        return self.get_id_from_obj(node_data)

    def get_id_from_obj(self, node_data):
        node_id = None
        for i in node_data['f']:
            if i['h'] != '':
                node_id = i['h']
        return node_id

    # ═══════════════════════════════════════════════════════════════
    # Account info
    # ═══════════════════════════════════════════════════════════════
    def get_user(self):
        return self._api_request({'a': 'ug'})

    def get_quota(self):
        """Return remaining disk quota in MB."""
        json_resp = self._api_request({
            'a': 'uq', 'xfer': 1, 'strg': 1, 'v': 1
        })
        return json_resp['mstrg'] / 1048576

    def get_storage_space(self, giga=False, mega=False, kilo=False):
        if sum(1 if x else 0 for x in (kilo, mega, giga)) > 1:
            raise ValueError("Only one unit prefix can be specified")
        unit_coef = 1
        if kilo:
            unit_coef = 1024
        if mega:
            unit_coef = 1048576
        if giga:
            unit_coef = 1073741824
        json_resp = self._api_request({'a': 'uq', 'xfer': 1, 'strg': 1})
        return {
            'used': json_resp['cstrg'] / unit_coef,
            'total': json_resp['mstrg'] / unit_coef,
        }

    def get_balance(self):
        """Get account balance (Pro only)."""
        user_data = self._api_request({"a": "uq", "pro": 1})
        if 'balance' in user_data:
            return user_data['balance']

    # ═══════════════════════════════════════════════════════════════
    # Delete / move
    # ═══════════════════════════════════════════════════════════════
    def delete(self, public_handle):
        return self.move(public_handle, 4)

    def delete_url(self, url):
        path = self._parse_url(url).split('!')
        public_handle = path[0]
        file_id = self.get_id_from_public_handle(public_handle)
        return self.move(file_id, 4)

    def destroy(self, file_id):
        return self._api_request({
            'a': 'd', 'n': file_id, 'i': self.request_id
        })

    def destroy_url(self, url):
        path = self._parse_url(url).split('!')
        public_handle = path[0]
        file_id = self.get_id_from_public_handle(public_handle)
        return self.destroy(file_id)

    def empty_trash(self):
        files = self.get_files_in_node(4)
        if files != {}:
            post_list = []
            for file in files:
                post_list.append({"a": "d", "n": file, "i": self.request_id})
            return self._api_request(post_list)

    def move(self, file_id, target):
        """Move a file to another parent node."""
        if type(target) == int:
            target_node_id = str(self.get_node_by_type(target)[0])
        elif type(target) in (str,):
            target_node_id = target
        else:
            file = target[1]
            target_node_id = file['h']
        return self._api_request({
            'a': 'm', 'n': file_id, 't': target_node_id,
            'i': self.request_id
        })

    # ═══════════════════════════════════════════════════════════════
    # Sharing / exporting
    # ═══════════════════════════════════════════════════════════════
    def get_upload_link(self, file):
        """Get a file's public link from an upload() response."""
        if 'f' in file:
            file = file['f'][0]
            public_handle = self._api_request({'a': 'l', 'n': file['h']})
            file_key = file['k'][file['k'].index(':') + 1:]
            decrypted_key = a32_to_base64(
                decrypt_key(base64_to_a32(file_key), self.master_key))
            return (f'{self.schema}://{self.domain}'
                    f'/#!{public_handle}!{decrypted_key}')
        else:
            raise ValueError('Upload() response required as input, '
                             'use get_link() for regular file input')

    def get_link(self, file):
        """Get a file public link from a file object."""
        file = file[1]
        if 'h' in file and 'k' in file:
            public_handle = self._api_request({'a': 'l', 'n': file['h']})
            if public_handle == -11:
                raise RequestError("Can't get a public link from that file "
                                   "(is this a shared file?)")
            decrypted_key = a32_to_base64(file['key'])
            return (f'{self.schema}://{self.domain}'
                    f'/#!{public_handle}!{decrypted_key}')
        else:
            raise ValidationError('File id and key must be present')

    def _node_data(self, node):
        try:
            return node[1]
        except (IndexError, KeyError):
            return node

    def get_folder_link(self, file):
        try:
            file = file[1]
        except (IndexError, KeyError):
            pass
        if 'h' in file and 'k' in file:
            public_handle = self._api_request({'a': 'l', 'n': file['h']})
            if public_handle == -11:
                raise RequestError("Can't get a public link from that file "
                                   "(is this a shared file?)")
            decrypted_key = a32_to_base64(file['shared_folder_key'])
            return (f'{self.schema}://{self.domain}'
                    f'/#F!{public_handle}!{decrypted_key}')
        else:
            raise ValidationError('File id and key must be present')

    def _export_file(self, node):
        node_data = self._node_data(node)
        self._api_request([{
            'a': 'l', 'n': node_data['h'], 'i': self.request_id
        }])
        return self.get_link(node)

    def export(self, path=None, node_id=None):
        nodes = self.get_files()
        if node_id:
            node = nodes[node_id]
        else:
            node = self.find(path)

        node_data = self._node_data(node)
        is_file_node = node_data['t'] == 0
        if is_file_node:
            return self._export_file(node)
        if node:
            try:
                return self.get_folder_link(node)
            except (RequestError, KeyError):
                pass

        master_key_cipher = AES.new(a32_to_str(self.master_key), AES.MODE_ECB)
        ha = base64_url_encode(
            master_key_cipher.encrypt(node_data['h'].encode("utf8") +
                                      node_data['h'].encode("utf8")))

        share_key = secrets.token_bytes(16)
        ok = base64_url_encode(master_key_cipher.encrypt(share_key))

        share_key_cipher = AES.new(share_key, AES.MODE_ECB)
        node_key = node_data['k']
        encrypted_node_key = base64_url_encode(
            share_key_cipher.encrypt(a32_to_str(node_key)))

        node_id = node_data['h']
        request_body = [{
            'a': 's2', 'n': node_id,
            's': [{'u': 'EXP', 'r': 0}],
            'i': self.request_id, 'ok': ok, 'ha': ha,
            'cr': [[node_id], [node_id], [0, 0, encrypted_node_key]]
        }]
        self._api_request(request_body)
        nodes = self.get_files()
        return self.get_folder_link(nodes[node_id])

    # ═══════════════════════════════════════════════════════════════
    # Download
    # ═══════════════════════════════════════════════════════════════
    def download(self, file, dest_path=None, dest_filename=None):
        """Download a file by its file object."""
        return self._download_file(file_handle=None,
                                   file_key=None,
                                   file=file[1],
                                   dest_path=dest_path,
                                   dest_filename=dest_filename,
                                   is_public=False)

    def download_url(self, url, dest_path=None, dest_filename=None):
        """Download a file by its public url."""
        path = self._parse_url(url).split('!')
        file_id = path[0]
        file_key = path[1]
        return self._download_file(
            file_handle=file_id,
            file_key=file_key,
            dest_path=dest_path,
            dest_filename=dest_filename,
            is_public=True,
        )

    def _download_file(self, file_handle, file_key, dest_path=None,
                       dest_filename=None, is_public=False, file=None):
        if file is None:
            if is_public:
                file_key = base64_to_a32(file_key)
                file_data = self._api_request({
                    'a': 'g', 'g': 1, 'p': file_handle
                })
            else:
                file_data = self._api_request({
                    'a': 'g', 'g': 1, 'n': file_handle
                })
            k = (file_key[0] ^ file_key[4], file_key[1] ^ file_key[5],
                 file_key[2] ^ file_key[6], file_key[3] ^ file_key[7])
            iv = file_key[4:6] + (0, 0)
            meta_mac = file_key[6:8]
        else:
            file_data = self._api_request({'a': 'g', 'g': 1, 'n': file['h']})
            k = file['k']
            iv = file['iv']
            meta_mac = file['meta_mac']

        if 'g' not in file_data:
            raise RequestError('File not accessible anymore')

        file_url = file_data['g']
        file_size = file_data['s']
        attribs = base64_url_decode(file_data['at'])
        attribs = decrypt_attr(attribs, k)

        if dest_filename is not None:
            file_name = dest_filename
        else:
            file_name = attribs['n']

        input_file = requests.get(file_url, stream=True).raw

        if dest_path is None:
            dest_path = ''
        else:
            dest_path += '/'

        with tempfile.NamedTemporaryFile(mode='w+b', prefix='megapy_',
                                         delete=False) as temp_output_file:
            k_str = a32_to_str(k)
            counter = Counter.new(
                128, initial_value=((iv[0] << 32) + iv[1]) << 64)
            aes = AES.new(k_str, AES.MODE_CTR, counter=counter)

            mac_str = '\0' * 16
            mac_encryptor = AES.new(k_str, AES.MODE_CBC,
                                    mac_str.encode("utf8"))
            iv_str = a32_to_str([iv[0], iv[1], iv[0], iv[1]])

            for chunk_start, chunk_size in get_chunks(file_size):
                chunk = input_file.read(chunk_size)
                chunk = aes.decrypt(chunk)
                temp_output_file.write(chunk)

                encryptor = AES.new(k_str, AES.MODE_CBC, iv_str)
                for i in range(0, len(chunk) - 16, 16):
                    block = chunk[i:i + 16]
                    encryptor.encrypt(block)

                # fix for files under 16 bytes failing
                if file_size > 16:
                    i += 16
                else:
                    i = 0

                block = chunk[i:i + 16]
                if len(block) % 16:
                    block += b'\0' * (16 - (len(block) % 16))
                mac_str = mac_encryptor.encrypt(encryptor.encrypt(block))

                file_info = os.stat(temp_output_file.name)
                logger.info('%s of %s downloaded',
                            file_info.st_size, file_size)

            file_mac = str_to_a32(mac_str)
            if (file_mac[0] ^ file_mac[1],
                    file_mac[2] ^ file_mac[3]) != meta_mac:
                raise ValueError('Mismatched mac')

            output_path = Path(dest_path + file_name)
            shutil.move(temp_output_file.name, output_path)
            return output_path

    # ═══════════════════════════════════════════════════════════════
    # Upload
    # ═══════════════════════════════════════════════════════════════

    def upload(self, filename, dest=None, dest_filename=None,
               progress_callback=None):
        """
        Upload a file to Mega.

        progress_callback(uploaded_bytes, total_bytes) is called after each
        chunk when provided, so callers can display live progress.
        """
        # determine storage node
        if dest is None:
            if not hasattr(self, 'root_id'):
                self.get_files()
            dest = self.root_id

        # Accept both a node-id string and a dict like {"h": "..."}
        if isinstance(dest, dict):
            dest = dest.get("h", dest)

        with open(filename, 'rb') as input_file:
            file_size = os.path.getsize(filename)
            ul_url = self._api_request({'a': 'u', 's': file_size})['p']

            # generate random aes key (128) for file
            ul_key = [random.randint(0, 0xFFFFFFFF) for _ in range(6)]
            k_str = a32_to_str(ul_key[:4])
            count = Counter.new(
                128, initial_value=((ul_key[4] << 32) + ul_key[5]) << 64)
            aes = AES.new(k_str, AES.MODE_CTR, counter=count)

            upload_progress = 0
            completion_file_handle = None

            mac_str = '\0' * 16
            mac_encryptor = AES.new(k_str, AES.MODE_CBC,
                                    mac_str.encode("utf8"))
            iv_str = a32_to_str([ul_key[4], ul_key[5], ul_key[4], ul_key[5]])

            if file_size > 0:
                for chunk_start, chunk_size in get_chunks(file_size):
                    chunk = input_file.read(chunk_size)
                    upload_progress += len(chunk)

                    encryptor = AES.new(k_str, AES.MODE_CBC, iv_str)
                    for i in range(0, len(chunk) - 16, 16):
                        block = chunk[i:i + 16]
                        encryptor.encrypt(block)

                    # fix for files under 16 bytes failing
                    if file_size > 16:
                        i += 16
                    else:
                        i = 0

                    block = chunk[i:i + 16]
                    if len(block) % 16:
                        block += makebyte('\0' * (16 - len(block) % 16))
                    mac_str = mac_encryptor.encrypt(encryptor.encrypt(block))

                    # encrypt file and upload
                    chunk = aes.encrypt(chunk)
                    output_file = requests.post(
                        ul_url + "/" + str(chunk_start),
                        data=chunk,
                        timeout=self.timeout,
                    )
                    completion_file_handle = output_file.text

                    # debug-level, avoids flooding the log
                    logger.debug('%s of %s uploaded',
                                 upload_progress, file_size)

                    if progress_callback is not None:
                        try:
                            progress_callback(upload_progress, file_size)
                        except Exception:
                            logger.exception(
                                "progress_callback raised, ignoring")
            else:
                output_file = requests.post(ul_url + "/0", data='',
                                            timeout=self.timeout)
                completion_file_handle = output_file.text

            logger.info('Chunks uploaded (%s bytes)', file_size)
            logger.info('Setting attributes to complete upload')

            file_mac = str_to_a32(mac_str)
            meta_mac = (file_mac[0] ^ file_mac[1], file_mac[2] ^ file_mac[3])

            dest_filename = dest_filename or os.path.basename(filename)
            attribs = {'n': dest_filename}

            encrypt_attribs = base64_url_encode(
                encrypt_attr(attribs, ul_key[:4]))

            key = [
                ul_key[0] ^ ul_key[4], ul_key[1] ^ ul_key[5],
                ul_key[2] ^ meta_mac[0], ul_key[3] ^ meta_mac[1],
                ul_key[4], ul_key[5], meta_mac[0], meta_mac[1],
            ]
            encrypted_key = a32_to_base64(encrypt_key(key, self.master_key))

            data = self._api_request({
                'a': 'p',
                't': dest,
                'i': self.request_id,
                'n': [{
                    'h': completion_file_handle,
                    't': 0,
                    'a': encrypt_attribs,
                    'k': encrypted_key,
                }],
            })
            logger.info('Upload complete')
            return data    

    # ═══════════════════════════════════════════════════════════════
    # Folders / rename
    # ═══════════════════════════════════════════════════════════════
    def _mkdir(self, name, parent_node_id):
        ul_key = [random.randint(0, 0xFFFFFFFF) for _ in range(6)]
        attribs = {'n': name}
        encrypt_attribs = base64_url_encode(encrypt_attr(attribs, ul_key[:4]))
        encrypted_key = a32_to_base64(encrypt_key(ul_key[:4], self.master_key))

        return self._api_request({
            'a': 'p',
            't': parent_node_id,
            'n': [{
                'h': 'xxxxxxxx',
                't': 1,
                'a': encrypt_attribs,
                'k': encrypted_key,
            }],
            'i': self.request_id,
        })

    def _root_node_id(self):
        if not hasattr(self, 'root_id'):
            self.get_files()
        return self.root_id

    def create_folder(self, name, dest=None):
        dirs = tuple(d for d in str(name).split('/') if d)
        folder_node_ids = {}
        for idx, directory_name in enumerate(dirs):
            existing_node_id = self.find_path_descriptor(directory_name)
            if existing_node_id:
                folder_node_ids[idx] = existing_node_id
                continue
            if idx == 0:
                parent_node_id = (dest if dest is not None
                                  else self._root_node_id())
            else:
                parent_node_id = folder_node_ids[idx - 1]
            created_node = self._mkdir(directory_name, parent_node_id)
            folder_node_ids[idx] = created_node['f'][0]['h']
        return dict(zip(dirs, folder_node_ids.values()))

    def rename(self, file, new_name):
        file = file[1]
        attribs = {'n': new_name}
        encrypt_attribs = base64_url_encode(encrypt_attr(attribs, file['k']))
        encrypted_key = a32_to_base64(encrypt_key(file['key'],
                                                  self.master_key))
        return self._api_request([{
            'a': 'a',
            'attr': encrypt_attribs,
            'key': encrypted_key,
            'n': file['h'],
            'i': self.request_id,
        }])

    # ═══════════════════════════════════════════════════════════════
    # Contacts (basic)
    # ═══════════════════════════════════════════════════════════════
    def add_contact(self, email):
        return self._edit_contact(email, True)

    def remove_contact(self, email):
        return self._edit_contact(email, False)

    def _edit_contact(self, email, add):
        if add is True:
            l = '1'
        elif add is False:
            l = '0'
        else:
            raise ValidationError('add parameter must be of type bool')
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            raise ValidationError('add_contact requires a valid email address')
        return self._api_request({
            'a': 'ur', 'u': email, 'l': l, 'i': self.request_id
        })

    # ═══════════════════════════════════════════════════════════════
    # Public URL import
    # ═══════════════════════════════════════════════════════════════
    def get_public_url_info(self, url):
        file_handle, file_key = self._parse_url(url).split('!')
        return self.get_public_file_info(file_handle, file_key)

    def import_public_url(self, url, dest_node=None, dest_name=None):
        file_handle, file_key = self._parse_url(url).split('!')
        return self.import_public_file(file_handle, file_key,
                                       dest_node=dest_node,
                                       dest_name=dest_name)

    def get_public_file_info(self, file_handle, file_key):
        data = self._api_request({'a': 'g', 'p': file_handle, 'ssm': 1})
        if isinstance(data, int):
            raise RequestError(data)
        if 'at' not in data or 's' not in data:
            raise ValueError("Unexpected result", data)

        key = base64_to_a32(file_key)
        k = (key[0] ^ key[4], key[1] ^ key[5], key[2] ^ key[6],
             key[3] ^ key[7])
        size = data['s']
        unencrypted_attrs = decrypt_attr(base64_url_decode(data['at']), k)
        if not unencrypted_attrs:
            return None
        return {'size': size, 'name': unencrypted_attrs['n']}

    def import_public_file(self, file_handle, file_key,
                           dest_node=None, dest_name=None):
        if dest_node is None:
            dest_node = self.get_node_by_type(2)[1]
        if dest_name is None:
            pl_info = self.get_public_file_info(file_handle, file_key)
            dest_name = pl_info['name']

        key = base64_to_a32(file_key)
        k = (key[0] ^ key[4], key[1] ^ key[5], key[2] ^ key[6],
             key[3] ^ key[7])

        encrypted_key = a32_to_base64(encrypt_key(key, self.master_key))
        encrypted_name = base64_url_encode(encrypt_attr({'n': dest_name}, k))
        return self._api_request({
            'a': 'p',
            't': dest_node['h'],
            'n': [{
                'ph': file_handle,
                't': 0,
                'a': encrypted_name,
                'k': encrypted_key,
            }]
        })

    def create_folder_at(self, name, parent_node_id=None):
        """
        Create a single folder inside a specific parent node.
        If parent_node_id is None, create inside the root Cloud Drive.
        Returns the new folder's node id.
        """
        if parent_node_id is None:
            parent_node_id = self._root_node_id()
        created = self._mkdir(name, parent_node_id)
        return created['f'][0]['h']    