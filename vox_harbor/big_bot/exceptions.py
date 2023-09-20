class BigBotException(Exception):
    pass


class AlreadyJoinedError(BigBotException):
    pass


class TaskStepError(BigBotException):
    pass
