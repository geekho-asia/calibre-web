#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
import smtplib
import threading
from datetime import datetime
import logging
import time
import socket
import sys
import os
from email.generator import Generator
import web
from flask_babel import gettext as _
import re
import gdriveutils as gd
import subprocess

try:
    from StringIO import StringIO
    from email.MIMEBase import MIMEBase
    from email.MIMEMultipart import MIMEMultipart
    from email.MIMEText import MIMEText
except ImportError:
    from io import StringIO
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

from email import encoders
from email.utils import formatdate
from email.utils import make_msgid

chunksize = 8192

STAT_WAITING = 0
STAT_FAIL = 1
STAT_STARTED = 2
STAT_FINISH_SUCCESS = 3

TASK_EMAIL = 1
TASK_CONVERT = 2
TASK_UPLOAD = 3

RET_FAIL = 0
RET_SUCCESS = 1


# For gdrive download book from gdrive to calibredir (temp dir for books), read contents in both cases and append
# it in MIME Base64 encoded to
def get_attachment(bookpath, filename):
    """Get file as MIMEBase message"""
    calibrepath = web.config.config_calibre_dir
    if web.ub.config.config_use_google_drive:
        df = gd.getFileFromEbooksFolder(bookpath, filename)
        if df:
            datafile = os.path.join(calibrepath, bookpath, filename)
            if not os.path.exists(os.path.join(calibrepath, bookpath)):
                os.makedirs(os.path.join(calibrepath, bookpath))
            df.GetContentFile(datafile)
        else:
            return None
        file_ = open(datafile, 'rb')
        data = file_.read()
        file_.close()
        os.remove(datafile)
    else:
        try:
            file_ = open(os.path.join(calibrepath, bookpath, filename), 'rb')
            data = file_.read()
            file_.close()
        except IOError:
            web.app.logger.exception(e) # traceback.print_exc()
            web.app.logger.error(u'The requested file could not be read. Maybe wrong permissions?')
            return None

    attachment = MIMEBase('application', 'octet-stream')
    attachment.set_payload(data)
    encoders.encode_base64(attachment)
    attachment.add_header('Content-Disposition', 'attachment',
                          filename=filename)
    return attachment


# Class for sending email with ability to get current progress
class emailbase():

    transferSize = 0
    progress = 0

    def data(self, msg):
        self.transferSize = len(msg)
        (code, resp) = smtplib.SMTP.data(self, msg)
        self.progress = 0
        return (code, resp)

    def send(self, strg):
        """Send `strg' to the server."""
        if self.debuglevel > 0:
            print('send:', repr(strg), file=sys.stderr)
        if hasattr(self, 'sock') and self.sock:
            try:
                if self.transferSize:
                    lock=threading.Lock()
                    lock.acquire()
                    self.transferSize = len(strg)
                    lock.release()
                    for i in range(0, self.transferSize, chunksize):
                        if type(strg) == bytes:
                            self.sock.send((strg[i:i+chunksize]))
                        else:
                            self.sock.send((strg[i:i + chunksize]).encode('utf-8'))
                        lock.acquire()
                        self.progress = i
                        lock.release()
                else:
                    self.sock.sendall(strg.encode('utf-8'))
            except socket.error:
                self.close()
                raise smtplib.SMTPServerDisconnected('Server not connected')
        else:
            raise smtplib.SMTPServerDisconnected('please run connect() first')

    def getTransferStatus(self):
        if self.transferSize:
            lock2 = threading.Lock()
            lock2.acquire()
            value = int((float(self.progress) / float(self.transferSize))*100)
            lock2.release()
            return str(value) + ' %'
        else:
            return "100 %"


# Class for sending email with ability to get current progress, derived from emailbase class
class email(emailbase, smtplib.SMTP):

    def __init__(self, *args, **kwargs):
        smtplib.SMTP.__init__(self, *args, **kwargs)


# Class for sending ssl encrypted email with ability to get current progress, , derived from emailbase class
class email_SSL(emailbase, smtplib.SMTP_SSL):

    def __init__(self, *args, **kwargs):
        smtplib.SMTP_SSL.__init__(self, *args, **kwargs)


#Class for all worker tasks in the background
class WorkerThread(threading.Thread):

    def __init__(self):
        self._stopevent = threading.Event()
        threading.Thread.__init__(self)
        self.status = 0
        self.current = 0
        self.last = 0
        self.queue = list()
        self.UIqueue = list()
        self.asyncSMTP=None
        self.id = 0

    # Main thread loop starting the different tasks
    def run(self):
        while not self._stopevent.isSet():
            doLock = threading.Lock()
            doLock.acquire()
            if self.current != self.last:
                doLock.release()
                if self.queue[self.current]['typ'] == TASK_EMAIL:
                    self.send_raw_email()
                if self.queue[self.current]['typ'] == TASK_CONVERT:
                    self.convert_mobi()
                # TASK_UPLOAD is handled implicitly
                self.current += 1
            else:
                doLock.release()
            time.sleep(1)

    def stop(self):
        self._stopevent.set()

    def get_send_status(self):
        if self.asyncSMTP:
            return self.asyncSMTP.getTransferStatus()
        else:
            return "0 %"

    def delete_completed_tasks(self):
        for index, task in reversed(list(enumerate(self.UIqueue))):
            if task['progress'] == "100 %":
                # delete tasks
                self.queue.pop(index)
                self.UIqueue.pop(index)
                # if we are deleting entries before the current index, adjust the index
                self.current -= 1
        self.last = len(self.queue)

    def get_taskstatus(self):
        if self.current  < len(self.queue):
            if self.queue[self.current]['status'] == STAT_STARTED:
                if not self.queue[self.current]['typ'] == TASK_CONVERT:
                    self.UIqueue[self.current]['progress'] = self.get_send_status()
                self.UIqueue[self.current]['runtime'] = self._formatRuntime(
                                                        datetime.now() - self.queue[self.current]['starttime'])
        return self.UIqueue

    def convert_mobi(self):
        # convert book, and upload in case of google drive
        self.queue[self.current]['status'] = STAT_STARTED
        self.UIqueue[self.current]['status'] = _('Started')
        self.queue[self.current]['starttime'] = datetime.now()
        self.UIqueue[self.current]['formStarttime'] = self.queue[self.current]['starttime']
        filename=self.convert()
        if web.ub.config.config_use_google_drive:
            gd.updateGdriveCalibreFromLocal()
        if(filename):
            self.add_email(_(u'Send to Kindle'), self.queue[self.current]['path'], filename,
                       self.queue[self.current]['settings'], self.queue[self.current]['kindle'],
                       self.UIqueue[self.current]['user'], _(u"E-Mail: %s" % self.queue[self.current]['title']))

    def convert(self):
        error_message = None
        file_path = self.queue[self.current]['file_path']
        bookid = self.queue[self.current]['bookid']
        # check if converter-excecutable is existing
        if not os.path.exists(web.ub.config.config_converterpath):
            self._handleError(_(u"Convertertool %(converter)s not found", converter=web.ub.config.config_converterpath))
            return
        try:
            # check which converter to use kindlegen is "1"
            if web.ub.config.config_ebookconverter == 1:
                command = [web.ub.config.config_converterpath, u'"' + file_path + u'.epub"']
            else:
                # Linux py2.7 encode as list without quotes no empty element for parameters
                # linux py3.x no encode and as list without quotes no empty element for parameters
                # windows py2.7 encode as string with qoutes empty element for parameters is okay
                # windows py 3.x no encode and as string with qoutes empty element for parameters is okay
                # seperate handling for windows and linux
                if os.name == 'nt':
                    command = web.ub.config.config_converterpath + u' "' + file_path + u'.epub" "' + \
                              file_path + u'.mobi" ' + web.ub.config.config_calibre
                    if sys.version_info < (3, 0):
                        command = command.encode(sys.getfilesystemencoding())
                else:
                    command = [web.ub.config.config_converterpath, (file_path + u'.epub'),
                               (file_path + u'.mobi')]
                    if web.ub.config.config_calibre:
                        command.append(web.ub.config.config_calibre)
                    if sys.version_info < (3, 0):
                        command = [ x.encode(sys.getfilesystemencoding()) for x in command ]

            p = subprocess.Popen(command, stdout=subprocess.PIPE, universal_newlines=True)
        except OSError as e:
            self._handleError(_(u"Ebook-converter failed: %s" % e))
            return
        if web.ub.config.config_ebookconverter == 1:
            nextline = p.communicate()[0]
            # Format of error message (kindlegen translates its output texts):
            # Error(prcgen):E23006: Language not recognized in metadata.The dc:Language field is mandatory.Aborting.
            conv_error = re.search(".*\(.*\):(E\d+):\s(.*)", nextline, re.MULTILINE)
            # If error occoures, store error message for logfile
            if conv_error:
                error_message = _(u"Kindlegen failed with Error %(error)s. Message: %(message)s",
                                  error=conv_error.group(1), message=conv_error.group(2).strip())
            web.app.logger.debug("convert_kindlegen: " + nextline)

        else:
            while p.poll() is None:
                nextline = p.stdout.readline()
                if os.name == 'nt' and sys.version_info < (3, 0):
                    nextline = nextline.decode('windows-1252')
                web.app.logger.debug(nextline.strip('\r\n'))
                # parse progress string from calibre-converter
                progress = re.search("(\d+)%\s.*", nextline)
                if progress:
                    self.UIqueue[self.current]['progress'] = progress.group(1) + ' %'

        #process returncode
        check = p.returncode

        # kindlegen returncodes
        # 0 = Info(prcgen):I1036: Mobi file built successfully
        # 1 = Info(prcgen):I1037: Mobi file built with WARNINGS!
        # 2 = Info(prcgen):I1038: MOBI file could not be generated because of errors!
        if ( check < 2 and web.ub.config.config_ebookconverter == 1) or \
                (check == 0 and web.ub.config.config_ebookconverter == 2):
            cur_book = web.db.session.query(web.db.Books).filter(web.db.Books.id == bookid).first()
            new_format = web.db.Data(name=cur_book.data[0].name,book_format="MOBI",
                                     book=bookid,uncompressed_size=os.path.getsize(file_path + ".mobi"))
            cur_book.data.append(new_format)
            web.db.session.commit()
            self.queue[self.current]['path'] = cur_book.path
            self.queue[self.current]['title'] = cur_book.title
            if web.ub.config.config_use_google_drive:
                os.remove(file_path + u".epub")
            self.queue[self.current]['status'] = STAT_FINISH_SUCCESS
            self.UIqueue[self.current]['status'] = _('Finished')
            self.UIqueue[self.current]['progress'] = "100 %"
            self.UIqueue[self.current]['runtime'] = self._formatRuntime(
                                                    datetime.now() - self.queue[self.current]['starttime'])
            return file_path + ".mobi"
        else:
            web.app.logger.info("ebook converter failed with error while converting book")
            if not error_message:
                error_message = 'Ebook converter failed with unknown error'
            self._handleError(error_message)
            return


    def add_convert(self, file_path, bookid, user_name, typ, settings, kindle_mail):
        addLock = threading.Lock()
        addLock.acquire()
        if self.last >= 20:
            self.delete_completed_tasks()
        # progress, runtime, and status = 0
        self.id += 1
        self.queue.append({'file_path':file_path, 'bookid':bookid, 'starttime': 0, 'kindle':kindle_mail,
                           'status': STAT_WAITING, 'typ': TASK_CONVERT, 'settings':settings})
        self.UIqueue.append({'user': user_name, 'formStarttime': '', 'progress': " 0 %", 'type': typ,
                             'runtime': '0 s', 'status': _('Waiting'),'id': self.id } )
        self.id += 1

        self.last=len(self.queue)
        addLock.release()


    def add_email(self, subject, filepath, attachment, settings, recipient, user_name, typ):
        # if more than 20 entries in the list, clean the list
        addLock = threading.Lock()
        addLock.acquire()
        if self.last >= 20:
            self.delete_completed_tasks()
        # progress, runtime, and status = 0
        self.queue.append({'subject':subject, 'attachment':attachment, 'filepath':filepath,
                           'settings':settings, 'recipent':recipient, 'starttime': 0,
                           'status': STAT_WAITING, 'typ': TASK_EMAIL})
        self.UIqueue.append({'user': user_name, 'formStarttime': '', 'progress': " 0 %", 'type': typ,
                             'runtime': '0 s', 'status': _('Waiting'),'id': self.id })
        self.id += 1
        self.last=len(self.queue)
        addLock.release()

    def add_upload(self, user_name, typ):
        # if more than 20 entries in the list, clean the list
        addLock = threading.Lock()
        addLock.acquire()
        if self.last >= 20:
            self.delete_completed_tasks()
        # progress=100%, runtime=0, and status finished
        self.queue.append({'starttime': datetime.now(), 'status': STAT_FINISH_SUCCESS, 'typ': TASK_UPLOAD})
        self.UIqueue.append({'user': user_name, 'formStarttime': '', 'progress': "100 %", 'type': typ,
                             'runtime': '0 s', 'status': _('Finished'),'id': self.id })
        self.UIqueue[self.current]['formStarttime'] = self.queue[self.current]['starttime']
        self.id += 1
        self.last=len(self.queue)
        addLock.release()
    
        
    def send_raw_email(self):
        self.queue[self.current]['starttime'] = datetime.now()
        self.UIqueue[self.current]['formStarttime'] = self.queue[self.current]['starttime']
        self.queue[self.current]['status'] = STAT_STARTED
        self.UIqueue[self.current]['status'] = _('Started')
        obj=self.queue[self.current]
        # create MIME message
        msg = MIMEMultipart()
        msg['Subject'] = self.queue[self.current]['subject']
        msg['Message-Id'] = make_msgid('calibre-web')
        msg['Date'] = formatdate(localtime=True)
        text = _(u'This email has been sent via calibre web.')
        msg.attach(MIMEText(text.encode('UTF-8'), 'plain', 'UTF-8'))
        if obj['attachment']:
            result = get_attachment(obj['filepath'], obj['attachment'])
            if result:
                msg.attach(result)
            else:
                self._handleError(u"Attachment not found")
                return

        msg['From'] = obj['settings']["mail_from"]
        msg['To'] = obj['recipent']

        use_ssl = int(obj['settings'].get('mail_use_ssl', 0))

        # convert MIME message to string
        fp = StringIO()
        gen = Generator(fp, mangle_from_=False)
        gen.flatten(msg)
        msg = fp.getvalue()

        # send email
        try:
            timeout = 600  # set timeout to 5mins

            org_stderr = sys.stderr
            sys.stderr = StderrLogger()

            if use_ssl == 2:
                self.asyncSMTP = email_SSL(obj['settings']["mail_server"], obj['settings']["mail_port"], timeout)
            else:
                self.asyncSMTP = email(obj['settings']["mail_server"], obj['settings']["mail_port"], timeout)

            # link to logginglevel
            if web.ub.config.config_log_level != logging.DEBUG:
                self.asyncSMTP.set_debuglevel(0)
            else:
                self.asyncSMTP.set_debuglevel(1)
            if use_ssl == 1:
                self.asyncSMTP.starttls()
            if obj['settings']["mail_password"]:
                self.asyncSMTP.login(str(obj['settings']["mail_login"]), str(obj['settings']["mail_password"]))
            self.asyncSMTP.sendmail(obj['settings']["mail_from"], obj['recipent'], msg)
            self.asyncSMTP.quit()
            self.queue[self.current]['status'] = STAT_FINISH_SUCCESS
            self.UIqueue[self.current]['status'] = _('Finished')
            self.UIqueue[self.current]['progress'] = "100 %"
            self.UIqueue[self.current]['runtime'] = self._formatRuntime(
                                                        datetime.now() - self.queue[self.current]['starttime'])

            sys.stderr = org_stderr

        except (socket.error, smtplib.SMTPRecipientsRefused, smtplib.SMTPException) as e:
            self._handleError(e)
            return None

    def _formatRuntime(self, runtime):
        self.UIqueue[self.current]['rt'] = runtime.total_seconds()
        val = re.split('\:|\.', str(runtime))[0:3]
        erg = list()
        for v in val:
            if int(v) > 0:
                erg.append(v)
        retVal = (':'.join(erg)).lstrip('0') + ' s'
        if retVal == ' s':
            retVal = '0 s'
        return retVal
    
    def _handleError(self, error_message):
        web.app.logger.error(error_message)
        self.queue[self.current]['status'] = STAT_FAIL
        self.UIqueue[self.current]['status'] = _('Failed')
        self.UIqueue[self.current]['progress'] = "100 %"
        self.UIqueue[self.current]['runtime'] = self._formatRuntime(
                                                datetime.now() - self.queue[self.current]['starttime'])
        self.UIqueue[self.current]['message'] = error_message



class StderrLogger(object):

    buffer = ''

    def __init__(self):
        self.logger = web.app.logger

    def write(self, message):
        if message == '\n':
            self.logger.debug(self.buffer)
            print(self.buffer)
            self.buffer = ''
        else:
            self.buffer += message
