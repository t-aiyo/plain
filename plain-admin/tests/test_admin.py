from app.users.models import User

from plain.test import Client


def test_admin_login_required(db):
    client = Client()

    # Login required
    assert client.get("/admin/").status_code == 302

    user = User.objects.create(username="test")
    client.force_login(user)

    # Not admin yet
    assert client.get("/admin/").status_code == 404

    user.is_admin = True
    user.save()

    # Now admin (currently redirects to the first view)
    resp = client.get("/admin/")
    assert resp.status_code == 302
    assert resp.url == "/admin/p/session/"
