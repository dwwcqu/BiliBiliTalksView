"""Pure WBI request signing; keys are obtained at runtime."""

import hashlib
from urllib.parse import urlencode

_MIXIN = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


def sign_main(params: dict[str, str], img_key: str, sub_key: str,
              timestamp: int) -> dict[str, str]:
    if len(img_key) != 32 or len(sub_key) != 32 or type(timestamp) is not int:
        raise ValueError('invalid_signing_input')
    key = img_key + sub_key
    mixed = ''.join(key[index] for index in _MIXIN)[:32]
    values = {k: str(v) for k, v in params.items() if k not in ('wts', 'w_rid')}
    values['wts'] = str(timestamp)
    values = {k: ''.join(c for c in v if c not in "!'()*")
              for k, v in sorted(values.items())}
    digest = hashlib.md5((urlencode(values) + mixed).encode('utf-8')).hexdigest()
    return {**values, 'w_rid': digest}
