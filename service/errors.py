"""All exceptions thrown by the kgservice"""
from tapisservice.errors import BaseTapisError


class ResourceError(BaseTapisError):
    pass

class PermissionsException(BaseTapisError):
    pass
