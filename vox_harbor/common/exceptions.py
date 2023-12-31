import traceback

from fastapi import HTTPException, status


class BadRequestError(HTTPException):
    def __init__(self, msg: str) -> None:
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


class NotFoundError(HTTPException):
    def __init__(self, name: str) -> None:
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=name.capitalize() + ' not found')


def get_traceback_string(exception):
    if hasattr(exception, '__traceback__'):
        tb_strings = traceback.format_tb(exception.__traceback__)
    else:
        tb_strings = traceback.format_exception(*sys.exc_info())
    return ''.join(tb_strings)


def format_exception(e, with_traceback=False):
    if hasattr(e, '__module__'):
        exc_string = u'{}.{}: {}'.format(e.__module__, e.__class__.__name__, e)
    else:
        exc_string = u'{}: {}'.format(e.__class__.__name__, e)

    if with_traceback:
        traceback_string = ':\n' + get_traceback_string(exception=e)
    else:
        traceback_string = ''

    return u'{}{}'.format(exc_string, traceback_string)
