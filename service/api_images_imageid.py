from fastapi import APIRouter
from models_pods import Pod, UpdatePod, PodResponse, Password
from models_images import Image, ImageResponse, ImageDeleteResponse, UpdateImage
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok, error
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()

@router.delete(
    "/pods/images/{image_id:path}",
    tags=["Images"],
    summary="delete_image",
    operation_id="delete_image",
    response_model=ImageDeleteResponse)
async def delete_image(image_id):
    """
    Delete an image.

    Returns "".
    """
    logger.info(f"DELETE /pods/images/{image_id} - Top of delete_image.")

    # Needs to delete image
    image = Image.db_get_with_pk(image_id, tenant="siteadmintable", site=g.site_id)
    if not image:
        return error(result=f"", msg=f"Image with id {image_id} not found - not deleted.")
    image.db_delete(tenant="siteadmintable", site=g.site_id)

    return ok(result=str(image_id), msg="Image successfully deleted.")


@router.get(
    "/pods/images/{image_id:path}",
    tags=["Images"],
    summary="get_image",
    operation_id="get_image",
    response_model=ImageResponse | ImageDeleteResponse)
async def get_image(image_id):
    """
    Get an image.

    Returns retrieved image object.
    """
    logger.info(f"GET /pods/images/{image_id} - Top of get_image.")

    # TODO search
    image = Image.db_get_with_pk(image_id, tenant="siteadmintable", site=g.site_id)
    if not image:
        return error(result="", msg=f"Image with id {image_id} not found.")

    return ok(result=image.display(), msg="Image retrieved successfully.")

### Users MIGHT want to update image description. This code would be a good start.
#### /pods/images/{image_id}
# @router.put(
#     "/pods/images/{image_id}",
#     tags=["Images"],
#     summary="update_image",
#     operation_id="update_image",
#     response_model=ImageResponse)
# async def update_image(image_id, update_image: UpdateImage):
#     """
#     Update an image.
#     Note:
#     - Fields that change image source or sink are not modifiable. Please recreate your image in that case.
#     Returns updated image object.
#     """
#     logger.info(f"UPDATE /pods/images/{image_id} - Top of update_image.")
#     image = Image.db_get_with_pk(image_id, tenant=g.request_tenant_id, site=g.site_id)
#     pre_update_image = image.copy()
#     # Image existence is already checked above. Now we validate update and update with values that are set.
#     input_data = update_image.dict(exclude_unset=True)
#     for key, value in input_data.items():
#         setattr(image, key, value)
#     # Only update if there's a change
#     if image != pre_update_image:
#         image.db_update(tenant=g.request_tenant_id, site=g.site_id)
#     else:
#         return error(result=image.display(), msg="Incoming data made no changes to image. Is incoming data equal to current data?")

#     return ok(result=image.display(), msg="Image updated successfully.")

