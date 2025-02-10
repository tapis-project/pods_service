from fastapi import APIRouter, Query
from models_images import Image, ImagesResponse, ImageResponse, NewImage
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok
from codes import PermissionLevel
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)


router = APIRouter()


#### /pods/images

@router.get(
    "/pods/images",
    tags=["Images"],
    summary="get_images",
    operation_id="get_images",
    response_model=ImagesResponse)
async def get_images():
    """
    Get all images allowed globally + in respective tenant.
    
    Returns a list of images.
    """
    logger.info("GET /pods/images - Top of get_images.")

    # TODO search
    images =  Image.db_get_all(tenant="siteadmintable", site=g.site_id)
#    images =  Image.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)

    images_to_show = []
    for image in images:
        images_to_show.append(image.display())

    logger.info("Images retrieved.")
    return ok(result=images_to_show, msg="Images retrieved successfully.")


@router.post(
    "/pods/images",
    tags=["Images"],
    summary="add_image",
    operation_id="add_image",
    response_model=ImageResponse)
async def add_image(new_image: NewImage):
    """
    Add a image with inputted information.
    
    Returns new image object.
    """
    logger.info("POST /pods/images - Top of add_image.")

    # Create image object. Validates as well.
    image = Image(**new_image.dict())

    metadata = {}
    pre_new_image = new_image.image
    post_new_image = image.image
    if pre_new_image != post_new_image and ":" in pre_new_image:
        metadata = {"notice": "removed tag from image, tag enforcement does not yet exist"}

    # Create image database entry
    image.db_create(tenant="siteadmintable", site=g.site_id)
    logger.debug(f"New image saved in db. image: {image.display()}.")

    return ok(result=image.display(), msg="Images added successfully.", metadata=metadata)

#now add multiple images at once
@router.post(
    "/pods/images/bulk",
    tags=["Images"],
    summary="add_images",
    operation_id="add_images",
    response_model=ImagesResponse)
#I want new_images to be a dictionary o f images where image_name = {Image}
async def add_images(new_images: list[NewImage], skip_duplicates: bool = Query(False, description="Whether to skip duplicates or fail on duplicates.")): 
    """
    Add multiple images with inputted information.

    Returns new image objects.
    """
    logger.info("POST /pods/images/bulk - Top of add_images.")

    duplicate_images = []
    
    metadata = {}

    images = []
    for new_image in new_images:
        # Create image object. Validates as well.
        try:
            image = Image(**new_image.dict())
            image.db_create(tenant="siteadmintable", site=g.site_id)
            logger.debug(f"New image saved in db. image: {image.display()}.")
            images.append(image.display())
        except Exception as e:
            if 'duplicate key value violates unique constraint "image_pkey"' in e.args[0]:
                if skip_duplicates:
                    duplicate_images.append(new_image.image)
                    logger.debug(f"Skipping duplicate image: {new_image.image}.")
                    if len(duplicate_images) == 1:
                        metadata = {"notice": f"skipped 1 duplicate image: {duplicate_images[0]}"}
                    else:
                        metadata = {"notice": f"skipped {len(duplicate_images)} duplicate images: {', '.join(duplicate_images)}"}
                else:
                    # add a notice to use skip_duplicates if they want to skip duplicates in the error.
                    e.args = (f"{e.args[0]} Use skip_duplicates=True to skip duplicate errors and continue.".replace("\n", ""),)
                    raise e
            else:
                raise e

    if not images:
        msg = "No images added - all images were duplicates."
    elif len(images) == 1:
        msg = "Image added successfully."
    else:
        msg = "Images added successfully."
    return ok(result=images, metadata=metadata, msg=msg)