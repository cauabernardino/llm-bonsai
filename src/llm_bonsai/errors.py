class LabError(Exception):
    """An expected failure with a message the user can act on.

    The CLI prints it without a traceback; anything else is a bug and keeps its traceback.
    """
