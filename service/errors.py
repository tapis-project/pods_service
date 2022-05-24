"""All exceptions thrown by the pods_service"""
from tapisservice.errors import BaseTapisError


class ResourceError(BaseTapisError):
    pass

class PermissionsException(BaseTapisError):
    pass
