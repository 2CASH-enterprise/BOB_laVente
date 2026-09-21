from app.integrations.ecommerce.meta_catalog_client import MetaCatalogClient
from app.integrations.ecommerce.shopify_client import ShopifyClient


def get_shopify_client_factory():
    """Retourne une factory (shop_domain, access_token) -> ShopifyClient. Surchargeable en test."""
    return ShopifyClient


def get_meta_catalog_client_factory():
    """Retourne une factory (catalog_id, access_token) -> MetaCatalogClient. Surchargeable en test."""
    return MetaCatalogClient
