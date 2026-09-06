"""Safe storage failures, without data or connection details."""


class StorageError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
