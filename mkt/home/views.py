import jingo

from mkt.webapps.models import Webapp


# TODO: Cache this soooo hard.
def home(request):
    """The home page."""
    if not getattr(request, 'can_view_consumer', True):
        return jingo.render(request, 'home/home_walled.html')
    featured = Webapp.featured(None)[:9]
    return jingo.render(request, 'home/home.html', {
        'featured': featured,
    })
