# Knowledge Graph Service
Service to allow for easy deployment and use of databases. Able to import and export data from live databases. WIP. Better docs in the future.


## Task List
### FastAPI
- [X] Get FastAPI server running
- [X] Get global g object working
- [ ] Get auth pre-req working
	- [X] Middleware working
	- [ ] Flesh out the auth bit
- [X] Add SOME FastAPI exception handling
- [X] Create main api file and organize how routes will be organized
- [ ] Completely solve exception handling with FastAPI (don't let any errors out)
	- [X] Catch Exception and TapisErrors 
 	- [ ] Validation errors should be formatted better https://stackoverflow.com/a/69720977 Seems useful
- [ ] Finish all endpoints
 	- [ ] /pods
		- [ ] Create
			- [X] Have models and create messages
			- [ ] Finish the rest
		- [ ] Post
 	- [ ] /pods/{pod_id}
		- [ ] Del
		- [ ] Put
 	- [ ] /pods/{pod_id}/export
		- [ ] Post
 	- [ ] /data/{data_id}
		- [ ] Del
		- [ ] Put
- [ ] 	Validate everything
### Backend
- [ ] NeoStore
	- [X] Run function with validation
	- [ ] More helper fns
- [X] Channels.py
- [X] Queues.py
- [X] Request_Utils.py
- [ ] Kubernetes_utils.py
	- [ ] Organize file
- [ ] Spawner.py to create/deal with pods
- [ ] Health.py
- [ ] Caddy needs correct error message for 400-50X errors.
- [ ] Have different pod images for each database type
	- [X] Base Neo4j
	- [ ] ...
- [ ] Organize db better
- [ ] Connect S3
- [ ] Export scripts
- [ ] Init container for S3 import
### Nice to have
- [X] Create the best Makefile ever
- [ ] Make the README actually good
- [ ] Get config and entry and more root dir files mounted so they can be edited.
	- There's some permissions issues, for example, nothing can execute entry.sh when it's mounted. Config things as well.



## Makefile

Makefile for this repository that expects minikube, but allows for deployment templating, deploy, and cleaning up along with some dev tools and special operations using the variables located in the makefile and callable with `make vars`. Descriptions of the vars are in the Makefile.

```
# Get all commands and descriptions with:
cd /kg_service
make help
```

Generally devs would use `make clean up` over and over again. This will clean up dir and pods, build image, and deploy with minikube.

## Minikube

Everything here assumes minikube.

Minikube setup:
- Go through this setup guide: https://minikube.sigs.k8s.io/docs/start/
  - Makesure you get kubectl installed in step 3.

### Dev Container
You can use dev containers with Minikube with the Makefile!

Steps:
- Install minikube, VSCode, Kubernetes VSCode plugin, and Remote-Containers VSCode plugin (0.231.X does NOT work).
- With Kubernetes plugin in the VSCode sidebar:
  - clusters -> minikube -> Nodes -> minikube -> right-click kg-main pod -> Attach Visual Studio Code
    - This could error out. Read the error, but a lot of the time, you just need to refresh the cluster page because you forgot that you redeployed.
- Bonus fun:
  - Setting `DEV_TOOLS` to true in the Makefile will:
    - Mounts `{minikube}/kg_service` to the `kg-main` pod. Meaning you can work with persistent changes in the repo.
      - Particular Mounts:
        - `kg_service/service` -> `/home/tapis/service`
        - `kg_service/entry.sh` -> `/home/tapis/entry.sh`
      - Mounts with minikube require users first mount their volumes to the minikube internal mounts, do that with:
        - `minikube mount ~/kg_service:/kg_service`
        - You will have to keep the command running, no daemon mode.
  - Jupyter Lab auto started within main. Link should be in Make stdout under the up target.
