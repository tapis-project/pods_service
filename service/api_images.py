from fastapi import APIRouter, Query
from models_images import Image, ImagesResponse, ImageResponse, NewImage
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok
from codes import PermissionLevel
from errors import PermissionsException
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

    metadata = {}
    # Build allow list (needed for admins too for counting)
    main_tenants = ["tacc", "icicleai", "icicle", "dev", "astria", "a2cps", "scoped"]
    user_allow_list = []
    for allowed_image in images:
        tenants = allowed_image.tenants
        # If "-<tenant>" is present, restrict access for that tenant
        if g.username == allowed_image.added_by:
            # If the image was added by the user, allow it regardless of tenant
            user_allow_list.append(allowed_image)
            continue
        if f"-{g.request_tenant_id}" in tenants:
            continue
        # "**" allows all tenants
        if "**" in tenants:
            user_allow_list.append(allowed_image)
        # "*" allows only main_tenants
        elif "*" in tenants and g.request_tenant_id in main_tenants:
            user_allow_list.append(allowed_image)
        # Explicit tenant allow
        elif g.request_tenant_id in tenants:
            user_allow_list.append(allowed_image)

    # Only main tenants get config images
    if g.request_tenant_id in main_tenants:
        conf_images = conf.get('image_allow_list', [])
        for conf_img in conf_images:
            # If conf_img is a string, convert to dict with dummy/default fields
            if isinstance(conf_img, str):
                img_obj = Image(
                    image=conf_img,
                    tenants=["*"],
                    description="(from config)",
                    creation_ts=None,
                    added_by="system"
                )
            elif isinstance(conf_img, dict):
                # Fill missing fields with defaults
                img_obj = Image(
                    image=conf_img.get("image", ""),
                    tenants=conf_img.get("tenants", ["*"]),
                    description=conf_img.get("description", "(from config)"),
                    creation_ts=conf_img.get("creation_ts", None),
                    added_by=conf_img.get("added_by", "system")
                )
            else:
                continue
            user_allow_list.append(img_obj)

    # Admin mode: show all images, with metadata about what user normally sees
    if getattr(g, 'admin_active', False):
        custom_allow_list = list(images)
    else:
        custom_allow_list = user_allow_list

    # Remove duplicates by image name (favor DB images)
    seen = set()
    images_to_show = []
    for image in custom_allow_list:
        if image.image not in seen:
            images_to_show.append(image.display())
            seen.add(image.image)

    if getattr(g, 'admin_active', False):
        user_image_names = {img.image for img in user_allow_list}
        admin_only_count = sum(1 for img in images_to_show if img.get('image') not in user_image_names)
        metadata["admin_context"] = {
            "admin_mode": True,
            "user_accessible_images": sorted(user_image_names),
            "msg": f"You can access {len(images_to_show) - admin_only_count} images, admin reveals {admin_only_count}"
        }

    logger.info("Images retrieved.")
    return ok(result=images_to_show, metadata=metadata, msg="Images retrieved successfully.")


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

    # The image allowlist is a real control (pod create rejects images not on it),
    # and images live in the site-global siteadmintable with no per-object perms —
    # so, exactly like update_image (PUT), adding requires admin mode. Without this
    # any authenticated user could allowlist an arbitrary image (incl. tenants:["**"])
    # and run an unvetted container in the shared cluster.
    if not getattr(g, 'admin_active', False):
        raise PermissionsException("Adding images requires admin mode. Send X-Pods-Admin: true header.")

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

    if not getattr(g, 'admin_active', False):
        raise PermissionsException("Adding images requires admin mode. Send X-Pods-Admin: true header.")

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
            msg = e.args[0] if e.args and len(e.args) > 0 else str(e)
            if 'duplicate key value violates unique constraint "image_pkey"' in msg:
                if skip_duplicates:
                    duplicate_images.append(new_image.image)
                    logger.debug(f"Skipping duplicate image: {new_image.image}.")
                    if len(duplicate_images) == 1:
                        metadata = {"notice": f"skipped 1 duplicate image: {duplicate_images[0]}"}
                    else:
                        metadata = {"notice": f"skipped {len(duplicate_images)} duplicate images: {', '.join(duplicate_images)}"}
                else:
                    # add a notice to use skip_duplicates if they want to skip duplicates in the error.
                    e.args = (f"{msg} Use skip_duplicates=True to skip duplicate errors and continue.".replace("\n", ""),)
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