# Status codes for actor objects
ON = 'ON'
OFF = 'OFF'
RESTART = 'RESTART'

REQUESTED = 'REQUESTED'
SPAWNER_SETUP = 'SPAWNER SETUP'
CREATING_CONTAINER = 'CREATING CONTAINER'
CREATING_VOLUME = 'CREATING VOLUME'
#TODO: comment about order of states
COMPLETE = 'COMPLETE'
SUBMITTED = 'SUBMITTED'
RUNNING = 'RUNNING'
SHUTTING_DOWN = 'SHUTTING_DOWN'
STOPPED = 'STOPPED'
ERROR = 'ERROR'

class PermissionLevel(object):

    def __init__(self, name, level=None):
        self.name = name
        if level:
            self.level = level
        elif name == 'READ':
            self.level = 0
        elif name == 'USER':
            self.level = 1
        elif name == 'ADMIN':
            self.level = 2

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
        if self.name == 'READ':
            return ['READ', 'USER', 'ADMIN']
        elif self.name == 'USER':
            return ['USER', 'ADMIN']
        elif self.name == 'ADMIN':
            return ['ADMIN']
        else:
            raise KeyError(f"Found PermissionLevel name that is unknown. {self.name}")

READ = PermissionLevel('READ')
USER = PermissionLevel('USER')
ADMIN = PermissionLevel('ADMIN')


PERMISSION_LEVELS = (READ.name, USER.name, ADMIN.name)

# roles - only used when Tapis's JWT Auth is activated.
# the admin role allows users full access to Abaco, including modifying workers assigned to actors.
ADMIN_ROLE = 'pods_admin'

# the privileged role allows users to create privileged actors.
PRIVILEGED_ROLE = 'pods_privileged'

roles = [ADMIN_ROLE, PRIVILEGED_ROLE]