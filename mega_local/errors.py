class ValidationError(Exception):
    """Error in validation stage."""
    pass


_CODE_TO_DESCRIPTIONS = {
    -1: ('EINTERNAL',
         'An internal error has occurred. Please submit a bug report.'),
    -2: ('EARGS', 'You have passed invalid arguments to this command'),
    -3: ('EAGAIN',
         'A temporary congestion or server malfunction prevented your request. Retry.'),
    -4: ('ERATELIMIT',
         'You have exceeded your command weight per time quota.'),
    -5: ('EFAILED', 'The upload failed. Please restart it from scratch'),
    -6: ('ETOOMANY',
         'Too many concurrent IP addresses are accessing this upload target URL'),
    -7: ('ERANGE', 'The upload file packet is out of range'),
    -8: ('EEXPIRED', 'The upload target URL you are trying to access has expired.'),
    -9: ('ENOENT', 'Object (typically, node or user) not found'),
    -10: ('ECIRCULAR', 'Circular linkage attempted'),
    -11: ('EACCESS', 'Access violation'),
    -12: ('EEXIST', 'Trying to create an object that already exists'),
    -13: ('EINCOMPLETE', 'Trying to access an incomplete resource'),
    -14: ('EKEY', 'A decryption operation failed'),
    -15: ('ESID', 'Invalid or expired user session, please relogin'),
    -16: ('EBLOCKED', 'User blocked'),
    -17: ('EOVERQUOTA', 'Request over quota'),
    -18: ('ETEMPUNAVAIL', 'Resource temporarily not available, please try again later'),
    -19: ('ETOOMANYCONNECTIONS', 'many connections on this resource'),
    -20: ('EWRITE', 'Write failed'),
    -21: ('EREAD', 'Read failed'),
    -22: ('EAPPKEY', 'Invalid application key; request not processed'),
}


class RequestError(Exception):
    """Error in API request."""
    def __init__(self, code):
        self.code = code
        code_desc, long_desc = _CODE_TO_DESCRIPTIONS.get(
            code, ('EUNKNOWN', f'Unknown error code: {code}')
        )
        self.message = f'{code_desc}, {long_desc}'

    def __str__(self):
        return self.message