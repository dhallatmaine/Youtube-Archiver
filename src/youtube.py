import subprocess
import os
import urllib2
import re
from flask import Flask, request, abort, redirect, Response, url_for, render_template, jsonify
from flask_login import LoginManager, login_required, UserMixin, login_user, logout_user
from bs4 import BeautifulSoup
from celery import Celery

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret_key'
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

# Celery configuration
app.config['CELERY_BROKER_URL'] = 'redis://localhost:6379/0'
app.config['CELERY_RESULT_BACKEND'] = 'redis://localhost:6379/0'

# Initialize Celery
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

class User(UserMixin):
    def __init__(self , username , password , id , active=True):
        self.id = id
        self.username = username
        self.password = password
        self.active = active

    def get_id(self):
        return self.id

    def is_active(self):
        return self.active

    def get_auth_token(self):
        return make_secure_token(self.username , key='secret_key')

class UsersRepository:

    def __init__(self):
        self.users = dict()
        self.users_id_dict = dict()
        self.identifier = 0

    def save_user(self, user):
        self.users_id_dict.setdefault(user.id, user)
        self.users.setdefault(user.username, user)

    def get_user(self, username):
        return self.users.get(username)

    def get_user_by_id(self, userid):
        return self.users_id_dict.get(userid)

    def next_index(self):
        self.identifier +=1
        return self.identifier

users_repository = UsersRepository()
me = User('vecowski', 'password', users_repository.next_index())
users_repository.save_user(me)

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/login' , methods=['GET' , 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        registeredUser = users_repository.get_user(username)
        if registeredUser != None and registeredUser.password == password:
            login_user(registeredUser)
            return redirect(url_for('index'))
        else:
            error = 'Invalid username or password'
    return render_template('login.html', error = error)

@app.route('/download', methods=['POST'])
@login_required
def download():
    json = request.get_json()
    youtube_link = json['youtube_link']
    audio_only = json['audio']

    soup = BeautifulSoup(urllib2.urlopen(youtube_link), features="html.parser")
    title = soup.title.string

    if audio_only:
        cmd = ['sudo', 'youtube-dl', '-f', 'bestaudio[ext=m4a]', youtube_link]
    else:
        cmd = ['sudo', 'youtube-dl', '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4', youtube_link]

    task = video_download.apply_async(args=[cmd, title, audio_only])
    return jsonify({}), 202, {'Location': url_for('taskstatus', task_id=task.id)}

@celery.task(bind=True)
def video_download(self, cmd, title, audio_only):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True)
    current = 0
    stage = 'video'
    percent_ratio = 2.5
    audio_start = 40
    if (audio_only):
        percent_ratio = 1.25
        audio_start = 0

    for line in iter(proc.stdout.readline, ''):
        l = line.strip()
        if l.startswith('[download] Destination') and l.endswith('.mp4'):
            current = 0
            message = 'Starting download of video'
            self.update_state(state='PROGRESS', meta={'current': 0, 'status': message, 'title': title})
        elif l.startswith('[download] Destination') and l.endswith('.m4a'):
            current = 40
            stage = 'audio'
            message = 'Starting download of audio'
            self.update_state(state='PROGRESS', meta={'current': 40, 'status': message, 'title': title})
        elif l.startswith('[ffmpeg] Merging'):
            current = 85
            message = 'Merging video and audio'
            self.update_state(state='PROGRESS', meta={'current': 85, 'status': message, 'title': title})
        elif l.startswith('Deleting original file') and l.endswith('.mp4 (pass -k to keep)'):
            current = 90
            message = 'Clean up original video file'
            self.update_state(state='PROGRESS', meta={'current': 90, 'status': message, 'title': title})
        elif l.startswith('Deleting original file') and l.endswith('.m4a (pass -k to keep)'):
            current = 95
            message = 'Clean up original audio file'
            self.update_state(state='PROGRESS', meta={'current': 95, 'status': message, 'title': title})
        else:
            percent = re.findall('\s(100|(\d{1,2}(\.\d+)*))%', l)
            if len(percent) > 0 and len(percent[0]) > 0:
                per = float(percent[0][0]) / percent_ratio
                if (stage == 'audio'):
                    per = (float(percent[0][0]) / percent_ratio) + audio_start
                current = current + (per - current)
                message = 'Downloading ' + stage
                self.update_state(state='PROGRESS', meta={'current': current, 'status': message, 'title': title})
    proc.communicate()
    return {'current': 100, 'status': 'Download complete', 'title': title}

@app.route('/status/<task_id>')
@login_required
def taskstatus(task_id):
    task = video_download.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'current': 0,
            'status': 'Pending...',
            'title': 'Fetching title...'
        }
    elif task.state != 'FAILURE':
        response = {
            'state': task.state,
            'current': task.info.get('current', 0),
            'status': task.info.get('status', ''),
            'title': task.info.get('title')
        }
    else:
        response = {
            'state': task.state,
            'current': 100,
            'status': str(task.info),
            'title': task.info.get('title')
        }
    return jsonify(response)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# handle login failed
@app.errorhandler(401)
def page_not_found(e):
    return Response('<p>Login failed</p>')

# callback to reload the user object
@login_manager.user_loader
def load_user(userid):
    return users_repository.get_user_by_id(userid)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8066, debug=False)
