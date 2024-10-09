import os
import psutil
import pty
import select
import subprocess
import atexit
import fcntl
from flask import Flask, render_template, jsonify, request, send_file
from flask_socketio import SocketIO, emit

app = Flask(__name__)
socketio = SocketIO(app, async_mode='threading')

BASE_DIR = '/'
terminal_processes = {}
terminal_sessions = {}
active_terminal = None

@app.route('/')
def home():
    return render_template('dashboard.html')

@app.route('/api/system-info')
def system_info():
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
    except:
        cpu_usage = 'N/A'

    memory = psutil.virtual_memory()
    disk_usage = psutil.disk_usage('/')

    try:
        temp = psutil.sensors_temperatures()
        cpu_temp = temp['coretemp'][0].current if 'coretemp' in temp else 'N/A'
    except:
        cpu_temp = 'N/A'

    return jsonify({
        "cpu": cpu_usage,
        "memory": {
            "total": memory.total,
            "available": memory.available,
            "used": memory.used,
            "percent": memory.percent
        },
        "disk": {
            "total": disk_usage.total,
            "used": disk_usage.used,
            "free": disk_usage.free,
            "percent": disk_usage.percent
        },
        "temperature": cpu_temp
    })

@app.route('/api/files', methods=['GET'])
def list_files():
    path = request.args.get('path', '')
    full_path = os.path.join(BASE_DIR, path)
    try:
        files = []
        for entry in os.listdir(full_path):
            entry_path = os.path.join(full_path, entry)
            is_dir = os.path.isdir(entry_path)
            files.append({"name": entry, "is_dir": is_dir})
        return jsonify({"files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/delete', methods=['DELETE'])
def delete_file():
    path = request.args.get('path', '')
    full_path = os.path.join(BASE_DIR, path)
    try:
        if os.path.isdir(full_path):
            os.rmdir(full_path)
        else:
            os.remove(full_path)
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/upload', methods=['POST'])
def upload_file():
    path = request.args.get('path', '')
    full_path = os.path.join(BASE_DIR, path)
    if not os.path.exists(full_path):
        os.makedirs(full_path)
    for uploaded_file in request.files.getlist('file'):
        uploaded_file.save(os.path.join(full_path, uploaded_file.filename))
    return jsonify({"message": "Files uploaded successfully"}), 200

@app.route('/api/download', methods=['GET'])
def download_file():
    path = request.args.get('path', '')
    full_path = os.path.join(BASE_DIR, path)
    try:
        return send_file(full_path, as_attachment=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/create-terminal', methods=['POST'])
def create_terminal():
    terminal_name = request.json.get('name')

    if terminal_name in terminal_processes:
        return jsonify({"error": f"Terminal {terminal_name} already exists."}), 400

    master, slave = pty.openpty()
    process = subprocess.Popen(['screen', '-S', terminal_name, '-m', 'bash'], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    
    fcntl.fcntl(master, fcntl.F_SETFL, os.O_NONBLOCK)

    terminal_processes[terminal_name] = master

    return jsonify({"message": f"Terminal {terminal_name} created successfully."}), 200

@app.route('/api/delete-terminal', methods=['POST'])
def delete_terminal():
    terminal_name = request.json.get('name')

    if terminal_name not in terminal_processes:
        return jsonify({"error": f"Terminal {terminal_name} not found."}), 404

    try:
        subprocess.run(['screen', '-S', terminal_name, '-X', 'quit'], check=True)
        del terminal_processes[terminal_name]
        return jsonify({"message": f"Terminal {terminal_name} deleted successfully."}), 200
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"Failed to delete terminal {terminal_name}: {str(e)}"}), 500

@app.route('/api/list-terminals', methods=['GET'])
def list_terminals():
    return jsonify({"terminals": list(terminal_processes.keys())}), 200

@socketio.on('connect_terminal')
def connect_terminal(data):
    global active_terminal
    terminal_name = data['name']
    print(f"Connecting to terminal: {terminal_name}")

    master_fd = terminal_processes.get(terminal_name)

    if not master_fd:
        emit('terminal_error', f"Terminal {terminal_name} not found.")
        return

    terminal_sessions[terminal_name] = request.sid

    if active_terminal and active_terminal != terminal_name:
        socketio.emit('disconnect_terminal', {'name': active_terminal}, to=terminal_sessions[active_terminal])

    active_terminal = terminal_name

    socketio.start_background_task(target=read_terminal_output, master_fd=master_fd, terminal_name=terminal_name)

def read_terminal_output(master_fd, terminal_name):
    while True:
        rlist, _, _ = select.select([master_fd], [], [], 1)
        if rlist:
            output = os.read(master_fd, 1024).decode('utf-8')
            if output:
                session_id = terminal_sessions.get(terminal_name)
                if session_id:
                    socketio.emit('terminal_output', {'output': output}, to=session_id)

        if active_terminal != terminal_name:
            break

@socketio.on('disconnect_terminal')
def disconnect_terminal(data):
    global active_terminal
    terminal_name = data['name']
    print(f"Disconnecting from terminal: {terminal_name}")

    if terminal_name in terminal_processes:
        os.system(f'screen -S {terminal_name} -X quit') 
        active_terminal = None
        terminal_sessions.pop(terminal_name, None)

@socketio.on('send_command')
def send_command(data):
    terminal_name = data['name']
    command = data['command'] + '\n'
    
    master_fd = terminal_processes.get(terminal_name)
    if master_fd:
        os.write(master_fd, command.encode('utf-8')) 

def cleanup_screens():
    for terminal_name in terminal_processes:
       subprocess.run(['screen', '-S', terminal_name, '-X', 'quit'], check=True)
       print(f'Closed terminal: {terminal_name}')
    print("All screen sessions have been terminated.")

atexit.register(cleanup_screens)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000)
