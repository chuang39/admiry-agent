"""The booking services Admiry knows, one list for every reader (A3p-5 batch 2 Task 1 fix round 1, ruling R1-e): a link
to one of them is the page's booking link, never a link away (landing's check, page_builder's Book button), and each of
them serves many businesses' pages, so for the listing rules two addresses there are one site only when they are the
same page (listings.SHARED_HOSTS): one business's Calendly page never makes another's place close. The bare hosts,
which landing and page_builder also match below, and the hosts under them that serve many businesses' booking pages,
which the listing rules match exactly. A leaf: it imports nothing, so both modules read it without a cycle."""

BOOKING_HOSTS = (
    "calendly.com",
    "acuityscheduling.com",
    "app.acuityscheduling.com",  # app.acuityscheduling.com/schedule.php?owner=…
    "squareup.com",
    "app.squareup.com",
    "book.squareup.com",  # book.squareup.com/appointments/…
    "square.site",  # square.site/book/…; a business's own Square Online site is its own subdomain
    "zocdoc.com",
    "housecallpro.com",
    "book.housecallpro.com",  # book.housecallpro.com/book/…
    "jobber.com",
)
