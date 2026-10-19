from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import get_settings

CSP_REPORT_PATH = "/api/v1/monitoring/csp-report"

# Lot 73 : origines extérieures réellement utilisées par les pages (polices Google de la vitrine, connexion
# WhatsApp/Facebook intégrée du tableau de bord, images https des produits et des QR codes).
# 'unsafe-inline' reste nécessaire : les pages ont des scripts et des onclick écrits dans le HTML.
CSP_DIRECTIVES = (
    ("default-src", "'self'"),
    ("script-src", "'self' 'unsafe-inline' https://connect.facebook.net"),
    ("style-src", "'self' 'unsafe-inline' https://fonts.googleapis.com"),
    ("font-src", "'self' data: https://fonts.gstatic.com"),
    ("img-src", "'self' data: blob: https:"),
    ("media-src", "'self' data: blob: https:"),
    ("connect-src", "'self' https://connect.facebook.net https://graph.facebook.com https://*.facebook.com"),
    ("frame-src", "'self' https://*.facebook.com https://*.facebook.net"),
    ("worker-src", "'self'"),
    ("manifest-src", "'self'"),
    ("object-src", "'none'"),
    ("base-uri", "'self'"),
    ("form-action", "'self'"),
    ("frame-ancestors", "'none'"),
)


def csp_policy() -> str:
    return "; ".join(f"{name} {value}" for name, value in CSP_DIRECTIVES) + f"; report-uri {CSP_REPORT_PATH}"


def csp_header_name() -> str:
    return "Content-Security-Policy" if get_settings().csp_enforce else "Content-Security-Policy-Report-Only"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Section 32. Le HTTPS lui-même est terminé par le reverse proxy (Caddy) en production,
    pas par cette application — mais ces en-têtes de durcissement sont indépendants de ça.
    Lot 73 : CSP sur les pages HTML, d'abord en « Report-Only » (CSP_ENFORCE=false), puis appliquée.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers[csp_header_name()] = csp_policy()
        return response
