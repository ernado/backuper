# coding=utf-8
import os
import datetime
import shutil
import logging
import codecs
import sys

from errors import BackupException, ProjectException
from config import get_config
from database import dump, generate_pgpass
from uploader import upload_files
from archivator import incremental_compress, compress, compress_file
from reports import send
from files import get_size


class TYPES(object):
    """
    Types of backup
    """
    daily = 'daily'
    monthly = 'monthly'
    _types = (daily, monthly)

    def __contains__(self, item):
        if item in self._types:
            return True
        return False


def get_backup_index(project_name, day, month, year=None, b_type=None):
    """
    @param project_name: Name of the project
    @return: backup unique index
    """
    assert type(project_name) is str
    assert type(day) is int
    assert type(month) is int
    assert type(year) is int

    if year is None:
        year = datetime.datetime.now().year

    if b_type is not None and b_type in (TYPES.daily, TYPES.monthly):
        t = b_type[0]
    else:
        t = get_backup_type(day)[0]

    return '{project}-{t}-{d:0>2}-{m:0>2}-{y}'.format(project=project_name,
                                                      t=t,
                                                      d=day,
                                                      m=month,
                                                      y=str(year)[-2:])


def get_current_index(project_name, b_type=None):
    now = datetime.datetime.now()
    day = now.day
    if b_type == TYPES.monthly:
        day = 1
    return get_backup_index(project_name, day, now.month, now.year, b_type)


def get_backup_type(day=None):
    if day is None:
        day = datetime.datetime.now().day
    if day == 1:
        return TYPES.monthly
    return TYPES.daily


class Project(object):
    def __init__(self, project_title, projects_folder):
        self.title = project_title
        self.folder = os.path.join(projects_folder, self.title)
        if not os.path.isdir(self.folder):
            raise ProjectException('Folder %s does not exist for project %s' % (project_title, self.folder))
        self.media_folder = os.path.join(self.folder, 'media')
        if not os.path.isdir(self.media_folder):
            raise ProjectException('Media folder %s does not exist for project %s' % (self.media_folder, project_title))

    def __str__(self):
        return self.title


class Backuper(object):
    def __init__(self, project_title, b_type=None):
        self.log = logging.getLogger(__name__)
        self.cfg = get_config(self.log)
        self.file_handler = None
        self.current_folder = ''
        self.output_tarfile = ''
        self.b_compress_log_f = None

        if b_type is None:
            b_type = get_backup_type()

        if b_type in (TYPES.daily, TYPES.monthly):
            self.b_type = b_type
        else:
            raise ValueError("Unknown backup type %s" % b_type)
        try:
            projects_folder = self.cfg.get('backuper', 'projects')
            self.project = Project(project_title, projects_folder)
        except ProjectException as e:
            raise BackupException('Unable to open project %s: %s' % (project_title, e))
        self.b_index = get_current_index(self.project.title, b_type)
        self.b_folder = self.cfg.get('backuper', 'backups')
        self.log_filename = os.path.join(self.b_folder, '%s-backup.log.txt' % self.b_index)
        open(self.log_filename, 'w').close()
        self.b_time = datetime.datetime.now()
        self.initiate_loggers()

    def compress_media(self):
        self.log.info('Collecting media files')
        output_media_tarfile = os.path.join(self.current_folder, 'media.tar')
        old_incremental_file = '%s.inc' % get_backup_index(self.project.title, 1, self.b_time.month, self.b_time.year)
        old_incremental_file = os.path.join(self.b_folder, old_incremental_file)

        if not os.path.isfile(old_incremental_file):
            open(old_incremental_file, 'w').close()

        incremental_file = old_incremental_file.replace('.inc', '.new.inc')

        shutil.copy(old_incremental_file, incremental_file)
        incremental_compress(self.project.media_folder, output_media_tarfile, incremental_file,
                             self.b_compress_log_f, self.log)
        self.log.info('Incremental media files archive size: %s' % get_size(output_media_tarfile))
        shutil.move(incremental_file, incremental_file.replace('.new.inc', '.inc'))

    def dump_compress_database(self):
        dump_file_path = os.path.join(self.current_folder, '%s.dump' % self.project)
        dump(self.project.title, open(dump_file_path, 'w'), self.log)
        self.log.info('Dumped to %s' % get_size(dump_file_path))

        self.log.info('Compressing database')
        dump_tarfile_path = '%s.tar.gz' % dump_file_path
        compress_file(dump_file_path, dump_tarfile_path, self.log)
        os.remove(dump_file_path)
        self.log.info('Compressed to %s' % get_size(dump_tarfile_path))

    def upload(self):
        self.log.info('Uploading to ftp server')
        upload_files([self.output_tarfile], self.cfg, self.log)
        self.log.info('Completed')

    def finalize(self, c_log):
        self.file_handler.close()
        log_info = open(self.log_filename).read()
        send('backup %s' % self.b_index, log_info, cfg=self.cfg, files=[c_log], logger=self.log)

    def clean_up(self):
        self.log.info('Removing temporary files')
        shutil.rmtree(self.current_folder)
        self.b_compress_log_f.close()

    def create_folders(self, b_folder):
        if not os.path.exists(b_folder):
            self.log.info('Creating folder %s for all backups' % b_folder)
            os.mkdir(b_folder)

        self.current_folder = os.path.join(b_folder, self.b_index)
        self.output_tarfile = os.path.join(b_folder, '%s.tar' % self.b_index)

        if not os.path.exists(self.current_folder):
            self.log.info('Creating folder %s for current backup' % self.current_folder)
            os.mkdir(self.current_folder)

    def compress_all(self):
        self.log.info('Compressing all to file')
        compress(self.current_folder, self.output_tarfile, self.b_compress_log_f, self.log)

    def backup(self):
        self.log.info('Starting %s backup of %s' % (self.b_type, self.project))

        backup_folder = self.cfg.get('backuper', 'backups')
        compress_log_filename = os.path.join(backup_folder, '%s-compress.txt' % self.b_index)
        self.b_compress_log_f = codecs.open(compress_log_filename, 'w', 'utf-8')

        self.create_folders(backup_folder)
        self.dump_compress_database()
        self.compress_media()
        self.compress_all()
        self.clean_up()
        self.upload()
        self.finalize(compress_log_filename)

    def initiate_loggers(self):
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', r'%d.%m.%y %H:%M:%S')
        handler = logging.StreamHandler()
        self.log.setLevel(logging.INFO)
        handler.setFormatter(formatter)
        self.log.addHandler(handler)
        self.file_handler = logging.FileHandler(self.log_filename)
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)
        self.log.addHandler(self.file_handler)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print 'Usage: backuper.py <project_name> backup <?m/d>'
        print 'or: backuper.py <project_name> restore <dd.mm(?.yy)>'
    generate_pgpass()
    project = sys.argv[1]
    b_mode = sys.argv[2]
    if b_mode == 'backup':
        b_type = None
        if len(sys.argv) > 3:
            b_type = sys.argv[3]
            for t in (TYPES.monthly, TYPES.daily):
                if t[0] == b_type[0] or t == b_type:
                    b_type = t
            if b_type not in (TYPES.monthly, TYPES.daily):
                print 'Incorrect backup type'
                exit(-1)
        b = Backuper(project, b_type)
        b.backup()
    else:
        print 'Not implemented'
