import glob
import logging
import os

from .bootstrap.bootstrapdir import CdromBootstrapDirectory
from .exceptions import CallError
from .image.bootstrap import clean_mounts, setup_chroot_basedir, umount_tmpfs_and_clean_chroot_dir
from .image.iso import install_iso_packages, make_iso_file
from .image.manifest import UPDATE_FILE
from .image.utils import get_image_version
from .utils.logger import get_logger
from .utils.paths import LOG_DIR, RELEASE_DIR


logger = logging.getLogger(__name__)


def build_iso():
    try:
        return build_impl()
    finally:
        clean_mounts()


def build_impl():
    iso_logger = get_logger('iso_logger', 'create_iso.log', 'w')
    logger.info('Building TrueNAS SCALE iso (%s/create_iso.log)', LOG_DIR)
    clean_mounts()
    for f in glob.glob(os.path.join(LOG_DIR, 'cdrom*')):
        os.unlink(f)

    if not os.path.exists(UPDATE_FILE):
        raise CallError('Missing rootfs image. Run \'make update\' first.')

    logger.debug('Bootstrapping CD chroot [ISO]')
    cdrom_bootstrap_obj = CdromBootstrapDirectory(iso_logger)
    cdrom_bootstrap_obj.setup()

    setup_chroot_basedir(cdrom_bootstrap_obj, cdrom_bootstrap_obj.logger)

    image_version = get_image_version()
    logger.debug('Image version identified as %r', image_version)
    logger.debug('Installing packages [ISO]')
    try:
        install_iso_packages(iso_logger)

        logger.debug('Creating ISO file [ISO]')
        make_iso_file(iso_logger)
    finally:
        umount_tmpfs_and_clean_chroot_dir()

    logger.info('Success! CD/USB: %s/TrueNAS-SCALE-%s.iso', RELEASE_DIR, image_version)
