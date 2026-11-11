"""Saving an uploaded document must not probe S3 for the name first.

WHAT BROKE
----------
`file_overwrite = False` makes django-storages ask "is this name taken?"
before every save, which is a `HeadObject` call. The deployment's bucket
credentials answer 403 to HeadObject, so `FileField.save` raised and every
document upload from the website ended in HTTP 500 -- while the presigned
direct-to-S3 path kept working, because signing a URL calls nothing. That
is why the mobile apps were fine and the first web upload was not.

The guard was worthless anyway: `PrefixedUUIDPath` names every upload
`uuid4().hex`, so no caller-supplied name can collide.

These tests assert the property rather than the setting, so flipping the
setting back without thinking fails here.
"""

import uuid

from base.media import PrefixedUUIDPath
from base.storage_backends import PrivateDocumentStorage, PublicMediaStorage


class TestNamesAreUnguessableAndUnique:
    def test_every_upload_path_is_a_fresh_uuid(self):
        path = PrefixedUUIDPath('license_docs')
        names = {path(None, 'scan.JPG') for _ in range(500)}

        assert len(names) == 500, 'upload paths collided'
        for name in names:
            prefix, _, leaf = name.partition('/')
            assert prefix == 'license_docs'
            stem, _, ext = leaf.partition('.')
            # Parses as a uuid4 hex, and the extension is lower-cased.
            uuid.UUID(hex=stem)
            assert ext == 'jpg'


class TestSavingDoesNotAskS3WhetherTheNameExists:
    """`get_available_name` must return without touching the network.

    If it calls `exists()`, django-storages issues HeadObject -- which this
    deployment's credentials refuse.
    """

    def _assert_no_probe(self, storage):
        probed = []

        # Any call to exists() is the failure. Patched on the INSTANCE so a
        # real connection is never opened either way.
        storage.exists = lambda name: probed.append(name) or False

        name = storage.get_available_name('license_docs/abc123.jpg')

        assert probed == [], (
            'storage probed S3 for the name before saving; the bucket '
            'answers 403 to HeadObject and the save will raise'
        )
        assert name == 'license_docs/abc123.jpg'

    def test_private_document_storage_does_not_probe(self):
        self._assert_no_probe(PrivateDocumentStorage())

    def test_public_media_storage_does_not_probe(self):
        self._assert_no_probe(PublicMediaStorage())
