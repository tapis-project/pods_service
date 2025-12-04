from fastapi import APIRouter
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()
