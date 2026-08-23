from django.conf import settings


def branding(request):
    return {
        "SITE_BRAND": getattr(settings, "SITE_BRAND", "ClickUp"),
        "SITE_MODULE_PREFIX": getattr(settings, "SITE_MODULE_PREFIX", "ClickUp"),
        "SITE_TEXT_LOGO": getattr(settings, "SITE_TEXT_LOGO", True),
        "SITE_URL": getattr(settings, "SITE_URL", ""),
        "SITE_CONTACT_PHONE": getattr(settings, "SITE_CONTACT_PHONE", ""),
        "SITE_CONTACT_EMAIL": getattr(settings, "SITE_CONTACT_EMAIL", ""),
    }
