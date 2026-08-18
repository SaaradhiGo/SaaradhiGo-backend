from storages.backends.s3boto3 import S3Boto3Storage

class PublicMediaStorage(S3Boto3Storage):
    default_acl = None
    file_overwrite = False
    querystring_auth = False


class PrivateDocumentStorage(S3Boto3Storage):
    default_acl = None
    file_overwrite = False
    querystring_auth = True


def public_media_storage():
    return PublicMediaStorage()


def private_document_storage():
    return PrivateDocumentStorage()
