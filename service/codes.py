# Status codes for actor objects
ON = 'ON'
OFF = 'OFF'
RESTART = 'RESTART'

REQUESTED = 'REQUESTED'
SPAWNER_SETUP = 'SPAWNER SETUP'
CREATING = 'CREATING'
#TODO: comment about order of states
COMPLETE = 'COMPLETE'
SUBMITTED = 'SUBMITTED'
AVAILABLE = 'AVAILABLE'
DELETING = 'DELETING'
STOPPED = 'STOPPED'
ERROR = 'ERROR'

class PermissionLevel(object):

    def __init__(self, name, level=None):
        self.name = name
        if level:
            self.level = level
        elif name == 'NONE':
            self.level = 0
        elif name == 'READ':
            self.level = 1
        elif name == 'USER':
            self.level = 2
        elif name == 'ADMIN':
            self.level = 3
        elif name == 'APPROVEDADMIN':
            self.level = 4

    def __lt__(self, other):
        if isinstance(other, PermissionLevel):
            return self.level.__lt__(other.level)
        return NotImplemented

    def __le__(self, other):
        if isinstance(other, PermissionLevel):
            return self.level.__le__(other.level)
        return NotImplemented

    def __gt__(self, other):
        if isinstance(other, PermissionLevel):
            return self.level.__gt__(other.level)
        return NotImplemented

    def __ge__(self, other):
        if isinstance(other, PermissionLevel):
            return self.level.__ge__(other.level)
        return NotImplemented

    def __repr__(self):
        return self.name

    def authorized_levels(self):
        match self.name:
            case 'NONE':
                return ['NONE', 'READ', 'USER', 'ADMIN', 'APPROVEDADMIN']
            case 'READ':
                return ['READ', 'USER', 'ADMIN', 'APPROVEDADMIN']
            case 'USER':
                return ['USER', 'ADMIN', 'APPROVEDADMIN']
            case 'ADMIN':
                return ['ADMIN', 'APPROVEDADMIN']
            case 'APPROVEDADMIN':
                return ['APPROVEDADMIN']
            case _:
                raise KeyError(f"Found PermissionLevel name that is unknown. {self.name}")

NONE = PermissionLevel('NONE')
READ = PermissionLevel('READ')
USER = PermissionLevel('USER')
ADMIN = PermissionLevel('ADMIN')
APPROVEDADMIN = PermissionLevel('APPROVEDADMIN')


PERMISSION_LEVELS = (NONE.name, READ.name, USER.name, ADMIN.name, APPROVEDADMIN.name)

# roles - only used when Tapis's JWT Auth is activated.
# the admin role allows users full access to Abaco, including modifying workers assigned to actors.
ADMIN_ROLE = 'pods_admin'

# the privileged role allows users to create privileged actors.
PRIVILEGED_ROLE = 'pods_privileged'

roles = [ADMIN_ROLE, PRIVILEGED_ROLE]