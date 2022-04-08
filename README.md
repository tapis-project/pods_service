# kg_service
WIP


## Makefile

Makefile for this repository that expects minikube, but allows for deployment templating, deploy, and cleaning up along with some dev tools and special operations using the variables located in the makefile and callable with `make vars`. Descriptions of the vars are in the Makefile.

```
# Get all commands and descriptions with:
cd /kg_service
make help
```

Generally devs would use `make clean up` over and over again. This will clean up dir and pods, build image, and deploy with minikube.


## Dev Container
You can use dev containers with Minikube! Yippee.

Steps:
- Install minikube, VSCode, Kubernetes VSCode plugin, and Remote-Containers VSCode plugin (0.231.X does NOT work).
- With Kubernetes plugin in the VSCode sidebar:
  - clusters -> minikube -> Nodes -> minikube -> right-click kg-main pod -> Attach Visual Studio Code
    - This could error out. Read the error, but a lot of the time, you just need to refresh the cluster page because you forgot that you redeployed.
- Bonus fun:
  - Setting `DEV_TOOLS` to true in the Makefile will:
    - Mount `{minikube}/kg_service` to `/home/tapis/service` in the `kg-main` pod. Meaning you can work with persistent changes in the repo. Mounts with minikube require users first mount their volumes to the minikube internal mounts, do that with:
      - `minikube mount ~/kg_service/service:/kg_service`
      - You will have to keep the command running, no daemon mode.
  - Jupyter Lab auto started within main. Link should be in Make stdout under the up target.
