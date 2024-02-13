# Description: This script is used to generate OpenAPI documentation for the Tapis Pods service.

import yaml
from api import api


### https://github.com/tiangolo/fastapi/issues/1140#issuecomment-880743503
### There's a bug in app.openapi() that doesn't handle AnyUrl correctly.
### I changed the representer a bit so there weren't extra quotes in outputs.
### Answer 1, Option B here shows that kind of https://stackoverflow.com/questions/76385999/how-to-export-a-pydantic-model-instance-as-yaml-with-url-type-as-string
from pydantic.networks import AnyUrl, url_regex

def _any_url_representer(dumper, data):
    print(data)
    return dumper.represent_str(str(data))
yaml.add_representer(AnyUrl, _any_url_representer)

# Write spec to file.
if __name__ == '__main__':
    print("Writing OpenAPI during startup.")
    with open("/home/tapis/docs/openapi_v3-pods.yml", 'w') as file:
        file.write(yaml.dump(api.openapi(), sort_keys=False))
