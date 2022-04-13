import json
import os
import socket
import time
import timeit
import datetime
import random

from kubernetes import client, config
from requests.packages.urllib3.exceptions import ReadTimeoutError
from requests.exceptions import ReadTimeout, ConnectionError

from tapisservice.logs import get_logger
logger = get_logger(__name__)

from channels import ExecutionResultsChannel
from tapisservice.config import conf
from codes import BUSY, READY, RUNNING
import encrypt_utils
import globals
from models import Actor, Execution, get_current_utc_time, display_time, site, ActorConfig
from stores import workers_store, alias_store, configs_store


# k8 client creation
config.load_incluster_config()
k8 = client.CoreV1Api()


host_id = os.environ.get('SPAWNER_HOST_ID', conf.spawner_host_id)
logger.debug(f"host_id: {host_id}")
host_ip = conf.spawner_host_ip

class KubernetesError(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message

class KubernetesStartContainerError(KubernetesError):
    pass

class KubernetesStopContainerError(KubernetesError):
    pass


def get_kubernetes_namespace():
    """
    Attempt to get namespace from filesystem
    Should be in file /var/run/secrets/kubernetes.io/serviceaccount/namespace
    
    We first take config, if not available, we grab from filesystem. Meaning
    config should usually be empty.
    """
    k8_namespace = conf.get("kubernetes_namespace", None)
    if not k8_namespace:
        try:
            logger.debug("Attempting to get kubernetes_namespace from file.")
            with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
                content = f.readlines()
                k8_namespace = content[0].strip()
        except Exception as e:
            logger.debug(f"Couldn't grab kubernetes namespace from filesystem. e: {e}")
        
    if not k8_namespace:
        msg = "In get_kubernetes_namespace(). Failed to get namespace."
        logger.debug(msg)
        raise KubernetesError(msg)
    logger.debug(f"In get_kubernetes_namespace(). Got namespace: {k8_namespace}.")
    return k8_namespace

# Get namespace for future use.
k8_namespace = get_kubernetes_namespace()


def rm_container(cid):
    """
    Remove a container.
    :param cid:
    :return:
    """
    k = client.CoreV1Api()
    
    try:
        k8.delete_namespaced_pod(name=cid, namespace=k8_namespace)
    except Exception as e:
        logger.info(f"Got exception trying to remove pod: {cid}. Exception: {e}")
        raise KubernetesError(f"Error removing pod {cid}, exception: {str(e)}")
    logger.info(f"pod {cid} removed.")


def list_all_containers():
    """Returns a list of all containers """
    k = client.CoreV1Api()
    #list pods in one namespace
    pods = k8.list_namespaced_pod(k8_namespace).items
    return pods


def get_current_worker_containers():
    worker_pods = []
    for pod in list_all_containers():
        pod_name = pod.metadata.name
        if 'actors-worker' in pod_name:
            # worker name format = "actors-worker-<tenant>-<actor-id>-<worker-id>
            # so split on '-' to get parts (containers use _, pods use -)
            try:
                parts = pod_name.split('-')
                tenant_id = parts[2]
                actor_id = parts[3]
                worker_id = parts[4]
                worker_pods.append({'pod': pod,
                                    'tenant_id': tenant_id,
                                    'actor_id': actor_id,
                                    'worker_id': worker_id})
            except Exception as e:
                msg = f"K8 Utils: Error getting current worker containers. e: {e}"
                logger.error(msg)
                raise Exception(msg)
    return worker_pods


def check_worker_pods_against_store():
    """
    Checks the existing worker pods on a host against the status of the worker in the workers_store.
    """
    worker_pods = get_current_worker_containers()
    for idx, w in enumerate(worker_pods):
        try:
            # try to get the worker from the store:
            store_key = f"{w['tenant_id']}_{w['actor_id']}_{w['worker_id']}"
            worker = workers_store[site()][store_key]
        except KeyError:
            worker = {}
        status = worker.get('status')
        try:
            last_execution_time = display_time(worker.get('last_execution_time'))
        except:
            last_execution_time = None
        print(idx, '). ', w['actor_id'], w['worker_id'], status, last_execution_time)


def container_running(name=None):
    """
    Check if there is a running pods whose name contains the string, `name`. Note that this function will
    return True if any running container has a name which contains the input `name`.
    """
    logger.debug("top of kubernetes_utils.container_running().")
    if not name:
        raise KeyError(f"kubernetes_utils.container_running received name: {name}")
    name = name.replace('_', '-').lower()
    try:
        if k8.read_namespaced_pod(namespace=k8_namespace, name=name).status.phase == 'Running':
            return True
    except client.rest.ApiException:
        # pod not found
        return False
    except Exception as e:
        msg = f"There was an error checking kubernetes_utils.container_running for name: {name}. Exception: {e}"
        logger.error(msg)
        raise KubernetesError(msg)
    

def run_container(image,
                  command,
                  name=None,
                  environment={},
                  mounts=[],
                  log_file=None,
                  auto_remove=False,
                  actor_id=None,
                  tenant=None,
                  api_server=None):
    """
    Run a container with access to Kubernetes cluster controls.
    Note: this function always mounts the abaco conf file so it should not be used by execute_actor().
    Note: this function gives express permissions to the pod to mess with kube, only used for workers.
    """
    logger.debug("top of kubernetes_utils.run_container().")
    k = client.CoreV1Api()

    # This should exist if config.json is using environment variables.
    # But there's a chance that it's not being used and isn't needed.
    abaco_host_path = os.environ.get('abaco_host_path')
    logger.debug(f"kubernetes_utils using abaco_host_path={abaco_host_path}")

    ## environment variables
    if 'abaco_host_path' not in environment:
        environment['abaco_host_path'] = abaco_host_path

    if 'actor_id' not in environment:
        environment['actor_id'] = actor_id

    if 'tenant' not in environment:
        environment['tenant'] = tenant

    if 'api_server' not in environment:
        environment['api_server'] = api_server


    # K8 testing stuff
    ### WORKER NAME NOTE
    # Worker name can be 123-abc, ., and -. No underscores! (lowercase only)
    name = name.replace('_', '-').lower()
    logger.debug(f"Starting kubernetes pod: {name}")
    
    ## Mounts
    # mount_example (volume type)
    # abaco config (configMap)
    # /work (hostPath)
    # Mounts require creating a volume and then mounting it. FYI.
    volumes = []
    volume_mounts = []
    
    
    
    
    
    # actors-config mount
    config_map = client.V1ConfigMapVolumeSource(name='actors-config')
    volumes.append(client.V1Volume(name='actors-config', config_map = config_map))
    volume_mounts.append(client.V1VolumeMount(name="actors-config", mount_path="/home/tapis/config.json", sub_path='config.json', read_only=True))
    
    
    logger.debug(f"volume_mounts: {volume_mounts}")


    # Logging file and directory location + mount
    # Not yet implemented. Kubernetes logs should just go to stdout, not a file. So no point to implement yet.

    # Create and start the pod
    try:
        # This is just a thing that's needed
        metadata = client.V1ObjectMeta(name=name)
        
        # Environment variable declaration
        env = []
        for env_name, env_val in environment.items():
            env.append(client.V1EnvVar(name=env_name, value=str(env_val)))
        env.append(client.V1EnvVar(name='abaco_host_path', value=os.environ.get('abaco_host_path')))
        env.append(client.V1EnvVar(name='_abaco_secret', value=os.environ.get('_abaco_secret')))
        # Password environment variable declaration
        mongo_pass_source = client.V1EnvVarSource(secret_key_ref = client.V1SecretKeySelector(name="tapis-abaco-secrets", key='mongo-password'))
        env.append(client.V1EnvVar(name="MONGO_PASSWORD", value_from=mongo_pass_source))
        rabbit_pass_source = client.V1EnvVarSource(secret_key_ref = client.V1SecretKeySelector(name="tapis-abaco-secrets", key='rabbitmq-password'))
        env.append(client.V1EnvVar(name="RABBITMQ_PASSWORD", value_from=rabbit_pass_source))
        service_pass_source = client.V1EnvVarSource(secret_key_ref = client.V1SecretKeySelector(name="tapis-abaco-secrets", key='service-password'))
        env.append(client.V1EnvVar(name="SERVICE_PASSWORD", value_from=service_pass_source))
        logger.debug(f"pod env variables: {env}")
        
        # Create container and pod
        containers = [client.V1Container(name=name, command=command, image=image, volume_mounts=volume_mounts, env=env)]
        pod_spec = client.V1PodSpec(service_account_name="actors-serviceaccount", restart_policy="Never", containers=containers, volumes=volumes)
        pod_body = client.V1Pod(metadata=metadata, spec=pod_spec, kind="Pod", api_version='v1')
        pod = k8.create_namespaced_pod(namespace=k8_namespace, body=pod_body)
    except Exception as e:
        msg = f"Got exception trying to start pod with image: {image}. Exception: {e}"
        logger.info(msg)
        raise KubernetesError(msg)
    logger.info(f"pod started successfully")
    return pod


def run_worker(image,
               revision,
               actor_id,
               worker_id,
               tenant,
               api_server):
    """
    Run an actor executor worker with a given channel and image.
    :return:
    """
    logger.debug("top of kubernetes_utils.run_worker()")
    command = ["python3", "-u", "/home/tapis/actors/kubernetes_worker.py"]
    logger.debug(f"kubernetes_utils running worker. actor_id: {actor_id}; worker_id: {worker_id};"
                 f"image:{image}; revision: {revision}; command: {command}")

    # mounts
    # Paths should be formatted as host_path:container_path for split

    volumes = []
    volume_mounts = []
    
    #for m in mounts:
    #binds[m.get('host_path')] = {'bind': m.get('container_path'),
    #                                'ro': m.get('format') == 'ro'}

    
    # actors-config mount
    config_map = client.V1ConfigMapVolumeSource(name='actors-config')
    volumes.append(client.V1Volume(name='actors-config', config_map = config_map))
    volume_mounts.append(client.V1VolumeMount(name="actors-config", mount_path="/home/tapis/config.json", sub_path='config.json', read_only=True))
    
    logger.debug(f"volume_mounts: {volume_mounts}")



    #mongo_certs_host_path_dir, mongo_certs_container_path_dir = conf.mongo_tls_certs_path.split(':')
    #logger.info(f"Using mongo certs paths - {mongo_certs_host_path_dir}:{mongo_certs_container_path_dir}")
    #mounts.append({'host_path': mongo_certs_host_path_dir,
    #                'container_path': mongo_certs_container_path_dir,
    #                'format': 'rw'})






    mounts = []
    ## mongo TLS certs - Read by all pods. Written only at init.
    if conf.mongo_tls:
        mongo_certs_host_path_dir, mongo_certs_container_path_dir = conf.mongo_tls_certs_path.split(':')
        logger.info(f"Using mongo certs paths - {mongo_certs_host_path_dir}:{mongo_certs_container_path_dir}")
        mounts.append({'k8_mount_name': "actors-mongo-tls-certs",
                       'host_path': mongo_certs_host_path_dir,
                       'container_path': mongo_certs_container_path_dir,
                       'format': 'rw'})

    ## fifo directory - Read by all pods. Written only by spawner.
    fifo_host_path_dir, fifo_container_path_dir = conf.worker_fifo_paths.split(':')
    logger.info(f"Using fifo paths - {fifo_host_path_dir}:{fifo_container_path_dir}")
    mounts.append({'k8_mount_name': 'actors-fifos-vol',
                   'host_path': os.path.join(fifo_host_path_dir, worker_id),
                   'container_path': os.path.join(fifo_container_path_dir, worker_id),
                   'format': 'rw'})

    ## sockets directory - Written to by all pods. Read by only worker(?)
    socket_host_path_dir, socket_container_path_dir = conf.worker_socket_paths.split(':')
    logger.info(f"Using socket paths - {socket_host_path_dir}:{socket_container_path_dir}")
    mounts.append({'host_path': os.path.join(socket_host_path_dir, worker_id),
                   'container_path': os.path.join(socket_container_path_dir, worker_id),
                   'format': 'rw'})

    logger.info(f"Final fifo and socket mounts: {mounts}")
    
    #### Should look into if k8 has any auto_remove akin thing to delete pods or jobs.
    #### ttlSecondsAfterFinished does exist for jobs.
    auto_remove = conf.worker_auto_remove
    pod = run_container(
        image=AE_IMAGE,
        command=command,
        environment={
            'image': image,
            'revision': revision,
            'worker_id': worker_id,
            'abaco_host_path': os.environ.get('abaco_host_path'),
            '_abaco_secret': os.environ.get('_abaco_secret')},
        mounts=mounts,
        log_file=None,
        auto_remove=auto_remove,
        name=f'actors_worker_{actor_id}_{worker_id}',
        actor_id=actor_id,
        tenant=tenant,
        api_server=api_server
    )
    # don't catch errors -- if we get an error trying to run a worker, let it bubble up.
    # TODO - determines worker structure; should be placed in a proper DAO class.
    logger.info(f"worker pod running. worker_id: {worker_id}.")
    return { 'image': image,
             # @todo - location will need to change to support swarm or cluster
             'location': "kubernetes",
             'id': worker_id,
             'cid': worker_id, #Pods don't have an id field
             'status': READY,
             'host_id': host_id,
             'host_ip': host_ip,
             'last_execution_time': 0,
             'last_health_check_time': get_current_utc_time() }

def stop_container(name):
    """
    Attempt to stop a running pod, with retry logic. Should only be called with a running pod.
    :param name: the pod name of the pod to be stopped.
    :return:
    """
    if not name:
        raise KeyError(f"kubernetes_utils.container_running received name: {name}")
    name = name.replace('_', '-').lower()

    k = client.CoreV1Api()

    i = 0
    while i < 10:        
        try:
            k8.delete_namespaced_pod(namespace=k8_namespace, name=name)
            return True
        except client.rest.ApiException:
            # pod not found
            return False
        except Exception as e:
            logger.error(f"Got another exception trying to stop the actor container. Exception: {e}")
            i += 1
            continue
    raise KubernetesStopContainerError


def execute_actor(actor_id,
                  worker_id,
                  execution_id,
                  image,
                  msg,
                  user=None,
                  environment={},
                  privileged=False,
                  mounts=[],
                  leave_container=False,
                  fifo_host_path=None,
                  socket_host_path=None,
                  mem_limit=None,
                  max_cpus=None,
                  tenant=None):
    """
    Creates and runs an actor pod and supervises the execution, collecting statistics about resource consumption
    with kubernetes utils.

    :param actor_id: the dbid of the actor; for updating worker status
    :param worker_id: the worker id; also for updating worker status
    :param execution_id: the id of the execution.
    :param image: the actor's image;
    :param msg: the message being passed to the actor.
    :param user: string in the form {uid}:{gid} representing the uid and gid to run the command as.
    :param environment: dictionary representing the environment to instantiate within the actor container.
    :param privileged: whether this actor is "privileged"; i.e., its container should run in privileged mode with the
    docker daemon mounted.
    :param mounts: list of dictionaries representing the mounts to add; each dictionary mount should have 3 keys:
    host_path, container_path and format (which should have value 'ro' or 'rw').
    :param fifo_host_path: If not None, a string representing a path on the host to a FIFO used for passing binary data to the actor.
    :param socket_host_path: If not None, a string representing a path on the host to a socket used for collecting results from the actor.
    :param mem_limit: The maximum amount of memory the Actor container can use; should be the same format as the --memory Docker flag.
    :param max_cpus: The maximum number of CPUs each actor will have available to them. Does not guarantee these CPU resources; serves as upper bound.
    :return: result (dict), logs (str) - `result`: statistics about resource consumption; `logs`: output from docker logs.
    """
    logger.debug(f"top of kubernetes_utils.execute_actor(); actor_id: {actor_id}; tenant: {tenant} (worker {worker_id}; {execution_id})")

    name=f'actors_exec_{actor_id}_{execution_id}'.replace('_', '-').lower()


    # get any configs for this actor
    actor_configs = {}
    config_list = []
    # list of all aliases for the actor
    alias_list = []
    # the actor_id passed in is the dbid
    actor_human_id = Actor.get_display_id(tenant, actor_id)

    for alias in alias_store[site()].items():
        logger.debug(f"checking alias: {alias}")
        if actor_human_id == alias['actor_id'] and tenant == alias['tenant']:
            alias_list.append(alias['alias'])
    logger.debug(f"alias_list: {alias_list}")
    # loop through configs to look for any that apply to this actor
    for config in configs_store[site()].items():
        # first look for the actor_id itself
        if actor_human_id in config['actors']:
            logger.debug(f"actor_id matched; adding config {config}")
            config_list.append(config)
        else:
            logger.debug("actor id did not match; checking aliases...")
            # if we didn't find the actor_id, look for ay of its aliases
            for alias in alias_list:
                if alias in config['actors']:
                    # as soon as we find it, append and get out (only want to add once)
                    logger.debug(f"alias {alias} matched; adding config: {config}")
                    config_list.append(config)
                    break
    logger.debug(f"got config_list: {config_list}")
    # for each config, need to check for secrets and decrypt ---
    for config in config_list:
        logger.debug('checking for secrets')
        try:
            if config['is_secret']:
                value = encrypt_utils.decrypt(config['value'])
                actor_configs[config['name']] = value
            else:
                actor_configs[config['name']] = config['value']
        except Exception as e:
            logger.error(f'something went wrong checking is_secret for config: {config}; e: {e}')

    logger.debug(f"final actor configs: {actor_configs}")
    environment['_actor_configs'] = actor_configs
    
    # We set all of these to overwrite default kubernetes env vars. There's two sets of default vars.
    # These, the required, and 
    environment['KUBERNETES_PORT'] = ""
    environment['KUBERNETES_SERVICE_HOST'] = ""
    environment['KUBERNETES_SERVICE_PORT'] = ""
    environment['KUBERNETES_SERVICE_PORT_HTTPS'] = ""
    environment['KUBERNETES_PORT_443_TCP'] = ""
    environment['KUBERNETES_PORT_443_TCP_ADDR'] = ""
    environment['KUBERNETES_PORT_443_TCP_PORT'] = ""
    environment['KUBERNETES_PORT_443_TCP_PROTO'] = ""

    # initially set the global force_quit variable to False
    globals.force_quit = False

    # initial stats object, environment and binds
    result = {'cpu': 0,
              'io': 0,
              'runtime': 0 }

    # instantiate kubernetes client
    k8 = client.CoreV1Api()

    # don't try to pass binary messages through the environment as these can cause
    # broken pipe errors. the binary data will be passed through the FIFO momentarily.
    if not fifo_host_path:
        environment['MSG'] = msg
    binds = {}


    # From docker_utils.py - If privileged, then mount docker daemon so container can create
    # more containers. We don't do that here, because kubernetes.
    # if privileged:
    # ...

    # add a bind key and dictionary as well as a volume for each mount
    for m in mounts:
        binds[m.get('host_path')] = {'bind': m.get('container_path'),
                                     'ro': m.get('format') == 'ro'}
        

    # mem_limit
    # -1 => unlimited memory
    if mem_limit == '-1':
        mem_limit = None

    # max_cpus
    try:
        max_cpus = int(max_cpus)
    except:
        max_cpus = None
    # -1 => unlimited cpus
    if max_cpus == -1:
        max_cpus = None

    # use retry logic since, when the compute node is under load, we see errors initially trying to create the socket
    # server object.
    keep_trying = True
    count = 0
    server = None
    logger.debug(f"results socket server instantiated. path: {socket_host_path} (worker {worker_id}; {execution_id})")

    # instantiate the results channel:
    results_ch = ExecutionResultsChannel(actor_id, execution_id)

    # create and start the container
    logger.debug(f"Final container environment: {environment};(worker {worker_id}; {execution_id})")
    logger.debug(f"Final binds: {binds} (worker {worker_id}; {execution_id})")

    # NEED TO ADD BINDS, PRIVILEGED     Done -> MEM_LIMIT, MAX_CPUS
    # Create and start the pod
    try:
        # This is just a thing that's needed
        metadata = client.V1ObjectMeta(name=name)
        
        # Environment variable declaration
        env = []
        for env_name, env_val in environment.items():
            env.append(client.V1EnvVar(name=env_name, value=str(env_val)))
        logger.debug(f"exec pod env variables: {env}")
        
        # Mem limit and max cpus - K8 resources
        resource_limits = {}
        # Memory
        # docker uses b/or no suffix, k/kb, m/mb, g/gb for units (technically also takes Ki, Mi, Gi fyi)
        # k8 uses no suffix (for bytes), Ki, Mi, Gi, Ti, Pi, or Ei (Does not accept kb, mb, or gb at all)
        # k/kb/ki->Ki, m/mb/mi->Mi, g/gb/gi->Gi
        if mem_limit:
            units = {'Ki': ['ki', 'kb', 'k'],
                     'Mi': ['mi', 'mb', 'm'],
                     'Gi': ['gi', 'gb', 'g']}
            converted = False
            for new_unit, old_units in units.items():
                for old_unit in old_units:
                    if old_unit in str(mem_limit):
                        mem_limit = mem_limit.replace(old_unit, new_unit)
                        resource_limits["memory"] =  mem_limit
                        converted = True
                        break
                if converted:
                    break
        # CPU
        # max_cpu should be a int representing nanocpus. We can use the k8 'n' suffix for nanocpus.
        if max_cpus:
            max_cpus = f"{max_cpus}n"
            resource_limits["cpu"] = max_cpus

        # Create resource and container
        if resource_limits:
            resources = client.V1ResourceRequirements(limits = resource_limits)
            container = client.V1Container(name=name, image=image, env=env, resources=resources)
        else:
            container = client.V1Container(name=name, image=image, env=env)

        # Security Context
        uid = None
        gid = None
        if user:
            try:
                # user should be None or 223232:323232
                uid, gid = user.split(":")
            except Exception as e:
                # error starting the pod, user will need to debug
                msg = f"Got exception getting user uid/gid: {e}; (worker {worker_id}; {execution_id})"
                logger.info(msg)
                raise KubernetesStartContainerError(msg)

        # Create container and pod body
        containers = [container]
        # Add in security context if uid and gid are found
        if uid and gid:
            security = client.V1SecurityContext(run_as_user=uid, run_as_group=gid)
            pod_spec = client.V1PodSpec(security_context=security, restart_policy="Never", containers=containers, enable_service_links=False)
        else:
            pod_spec = client.V1PodSpec(restart_policy="Never", containers=containers, enable_service_links=False)
        pod_body = client.V1Pod(metadata=metadata, spec=pod_spec, kind="Pod", api_version='v1')

        # Start pod
        # get the UTC time stamp
        start_time = get_current_utc_time()
        # start the timer to track total execution time.
        start = timeit.default_timer()
        logger.debug(f"right before k8.create_namespaced_pod: {start}; pod name: {name}; (worker {worker_id}; {execution_id})")
        pod = k8.create_namespaced_pod(namespace=k8_namespace, body=pod_body)
    except Exception as e:
        # if there was an error starting the pod, user will need to debug
        msg = f"Got exception starting actor exec pod: {e}; (worker {worker_id}; {execution_id})"
        logger.info(msg)
        raise KubernetesStartContainerError(msg)
    logger.info(f"pod started successfully")

    # Wait for pod to start running. Unique to kube as docker container start command is blocking. Not here.
    # To check for availablity we first check if pod is in Succeeded phase (possible with fast execs). If not we can't trust
    # pod.phase of "Running" as that means nothing. Need to look at container status. Container status is outputted as
    # state: {"running": None, "terminated": None, "waiting": None}
    # None is an object containing container_id, exit_code, start/finish time, message (error if applicable), reason, and signal.
    # Reason can be "CrashLoopBackOff" in waiting, "Completed" in terminated (should mean Succeeded pod phase),
    # "ContainerCreating" in waiting, etc. Only one state object is ever specified, we need to error in the case of errors,
    # and mark running if running.
    
    # local bool tracking whether the actor pod is still running
    running = False    
    loop_idx = 0
    while True:
        loop_idx += 1
        pod = k8.read_namespaced_pod(namespace=k8_namespace, name=name)
        pod_phase = pod.status.phase
        if pod_phase == "Succeeded":
            logger.debug(f"Kube exec in succeeded phase. (worker {worker_id}; {execution_id})")
            running = True
            break
        elif pod_phase == "Running":
            # Check if container running or in error state (exec pods are always only one container (so far))
            # Container can be in waiting state due to ContainerCreating ofc
            c_state = pod.status.container_statuses[0].state
            logger.debug(f'state: {c_state}')
            if c_state.waiting and c_state.waiting.reason != "ContainerCreating":
                logger.error(f"Found kube container waiting with reason: {c_state.waiting.reason} (worker {worker_id}; {execution_id})")
                stop_container(name)
                break
            elif c_state.running:
                running = True
                break
        
        # TODO: Add some more checks here, check for kube container error statuses.
        if loop_idx % 60:
            logger.debug(f"Waiting for kube exec to get to running. Been waiting {loop_idx} seconds; {loop_idx/60:.0f} minutes. (worker {worker_id}; {execution_id})")
        if loop_idx == 600:
            logger.debug(f"Waited 10 minutes for kube exec to get to ready, not ready still. shutting it down. (worker {worker_id}; {execution_id})")
            stop_container(name)
            break
        #time.sleep(1)
            
    if running:
        Execution.update_status(actor_id, execution_id, RUNNING)

    # Stats loop waiting for execution to end
    # a counter of the number of iterations through the main "running" loop;
    # this counter is used to determine when less frequent actions, such as log aggregation, need to run.
    loop_idx = 0
    log_ex = Actor.get_actor_log_ttl(actor_id)
    logs = None
    while running and not globals.force_quit:
        loop_idx += 1
        logger.debug(f"top of kubernetes_utils while running loop; loop_idx: {loop_idx}")

        # grab the logs every 3rd iteration --
        if loop_idx % 3 == 0:
            logs = None
            logs = k8.read_namespaced_pod_log(namespace=k8_namespace, name=name)
            Execution.set_logs(execution_id, logs, actor_id, tenant, worker_id, log_ex)

        # checking the pod status to see if it is still running ----
        if running:
            logger.debug(f"about to check pod status: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")
            # we need to wait for the pod to be available
            i = 0
            pod = None
            while i < 10:
                try:
                    pod = k8.read_namespaced_pod(namespace=k8_namespace, name=name)                    
                    break # if found properly
                except client.rest.ApiException:
                    # pod not found                    
                    logger.error(f"Got an IndexError trying to get the pod object. (worker {worker_id}; {execution_id})")
                    time.sleep(0.1)
                    i += 1
                
            logger.debug(f"done checking status: {timeit.default_timer()}; i: {i}; (worker {worker_id}; {execution_id})")
            # if we were never able to get the pod object, we need to stop processing and kill this
            # worker; the docker daemon could be under heavy load, but we need to not launch another
            # actor pod with this worker, because the existing pod may still be running,
            if i == 10 or not pod:
                # we'll try to stop the pod
                logger.error("Never could retrieve the pod object! Attempting to stop pod; "
                             f"name: {name}; (worker {worker_id}; {execution_id})")
                # kuberentes_utils.stop_container could raise an exception - if so, we let it pass up and have the worker
                # shut itself down.
                logger.info(f"pod {name} stopped. (worker {worker_id}; {execution_id})")

                # if we were able to stop the pod, we can set running to False and keep the
                # worker running
                running = False
                continue
            # Get pod state
            try:
                state = pod.status.phase
            except:
                state = "broken"
                logger.error(f"KUBE BUG: DON'T KNOW WHY THIS WOULD HAPPEN QUITE YET, couldn't get status.phase. pod: {pod}")
            # Work based on state
            if not state == 'Running':
                logger.debug(f"pod finished, final state: {state}; (worker {worker_id}; {execution_id})")
                running = False
                continue
            else:
                # pod still running; check if a force_quit has been sent OR
                # we are beyond the max_run_time
                runtime = timeit.default_timer() - start
                if globals.force_quit or (max_run_time > 0 and max_run_time < runtime):
                    logs = k8.read_namespaced_pod_log(namespace=k8_namespace, name=name)
                    if globals.force_quit:
                        logger.info(f"issuing force quit: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")
                    else:
                        logger.info(f"hit runtime limit: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")
                    stop_container(name)
                    running = False
            logger.debug(f"right after checking pod state: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")

    logger.info(f"pod stopped:{timeit.default_timer()}; (worker {worker_id}; {execution_id})")
    stop = timeit.default_timer()
    globals.force_quit = False

    # get info from pod execution, including exit code; Exceptions from any of these commands
    # should not cause the worker to shutdown or prevent starting subsequent actor pods.
    logger.debug("Pod finished")
    exit_code = 'undetermined'
    try:
        pod = k8.read_namespaced_pod(namespace=k8_namespace, name=name)
        try:
            c_state = pod.status.container_statuses[0].state # to be used to set final_state
            # Sets final state equal to whichever c_state object exists (only 1 exists at a time ever)
            pod_state = c_state.running or c_state.terminated or c_state.waiting
            pod_state = pod_state.to_dict()
            try:
                exit_code = pod_state.get('exit_code', 'No Exit Code')
                startedat_ISO = pod_state.get('started_at', 'No "started at" time (k8 - request feature on github)')
                finishedat_ISO = pod_state.get('finished_at' 'No "finished at" time (k8 - request feature on github)')
                # if times exist, converting ISO8601 times to unix timestamps
                if not 'github' in startedat_ISO:
                    # Slicing to 23 to account for accuracy up to milliseconds and replace to get rid of ISO 8601 'Z'
                    startedat_ISO = startedat_ISO.replace('Z', '')[:23]
                    pod_state['StartedAt'] = datetime.datetime.strptime(startedat_ISO, "%Y-%m-%dT%H:%M:%S.%f")

                if not 'github' in finishedat_ISO:
                    finishedat_ISO = pod.finishedat_ISO.replace('Z', '')[:23]
                    pod_state['FinishedAt'] = datetime.datetime.strptime(finishedat_ISO, "%Y-%m-%dT%H:%M:%S.%f")
            except Exception as e:
                logger.error(f"Datetime conversion failed for pod {name}. "
                             f"Exception: {e}; (worker {worker_id}; {execution_id})")
        except Exception as e:
            logger.error(f"Could not determine final state for pod {name}. "
                         f"Exception: {e}; (worker {worker_id}; {execution_id})")
            pod_state = {'unavailable': True}
    except client.rest.ApiException:
        logger.error(f"Could not get pod info for name: {name}. "
                     f"Exception: {e}; (worker {worker_id}; {execution_id})")

    logger.debug(f"right after getting pod object: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")
    # get logs from pod
    if not logs:
        logs = k8.read_namespaced_pod_log(namespace=k8_namespace, name=name)
    if not logs:
        # there are issues where container do not have logs associated with them when they should.
        logger.info(f"Pod: {name} had NO logs associated with it. (worker {worker_id}; {execution_id})")
    logger.debug(f"right after getting pod logs: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")

    #if socket_host_path:
    #    server.close()
    #    os.remove(socket_host_path)
    logger.debug(f"right after removing socket: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")

    # remove actor container with retrying logic -- check for specific filesystem errors from kube
    if not leave_container:
        logger.debug("deleting container")
        keep_trying = True
        count = 0
        while keep_trying and count < 10:
            keep_trying = False
            count = count + 1
            try:
                k8.delete_namespaced_pod(namespace=k8_namespace, name=name)
                logger.info(f"Actor pod removed. (worker {worker_id}; {execution_id})")
            except Exception as e:
                # if the pod is already gone we definitely want to quit:
                if "Reason: Not Found" in str(e):
                    logger.info("Got 'Not Found' exception - quiting. "
                                f"Exception: {e}; (worker {worker_id}; {execution_id})")
                    break
                else:
                    logger.error("Unexpected exception trying to remove actor pod. Giving up."
                                 f"Exception: {e}; type: {type(e)}; (worker {worker_id}; {execution_id})")
    else:
        logger.debug(f"leaving actor pod since leave_container was True. (worker {worker_id}; {execution_id})")
    logger.debug(f"right after removing actor container: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")

    result['runtime'] = int(stop - start)
    logger.debug(f"right after removing fifo; about to return: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")
    return result, logs, pod_state, exit_code, start_time
