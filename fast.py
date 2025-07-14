from flask import Flask, request, jsonify, send_file
import redis
import logging
import os
import json
import time
from threading import Thread

app = Flask(__name__)
rdb = redis.Redis(host='127.0.0.1', port=6379, db=0, decode_responses=True)

logging.basicConfig(level=logging.INFO)

def write_to_disk_in_background(path, content):
    """Function to run in a background thread to handle filesystem writes."""
    try:
        dir_path = os.path.dirname(path)
        os.makedirs(dir_path, exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        app.logger.info(f"Background write successful for {path}")
    except Exception as e:
        app.logger.error(f"Background write failed for {path}: {e}")

def create_user_html(user_id, message):
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>User Page - {user_id}</title>
</head>
<body>
    <h1>Welcome back, {user_id}!</h1>
    <p>Your message: {message}</p>
    <a href="/fast">Go back to main page</a>
</body>
</html>"""
    
    filename = f"{user_id}.html"
    with open(filename, 'w') as f:
        f.write(html_content)
    
    # Save to Redis
    rdb.set(f"user:{user_id}:html", html_content)
    rdb.set(f"user:{user_id}:message", message)
    app.logger.info(f"Saved user {user_id} data to Redis")
    
    return filename

def get_highest_project_number():
    """Get the highest project number from Redis."""
    highest = rdb.get("projects:highest_number")
    return int(highest) if highest else 0

def get_projects_info():
    """Get projects information from Redis, with a filesystem fallback."""
    projects_json = rdb.get("projects:list")
    if projects_json:
        try:
            return json.loads(projects_json)
        except json.JSONDecodeError:
            app.logger.error("Could not decode projects:list from Redis. Falling back to filesystem.")
            # Clear the bad key
            rdb.delete("projects:list")

    app.logger.info("projects:list not found or was invalid in Redis, falling back to filesystem scan.")
    projects_info = []
    projects_dir = "projects"
    
    if os.path.exists(projects_dir):
        for item in os.listdir(projects_dir):
            item_path = os.path.join(projects_dir, item)
            if os.path.isdir(item_path) and item.startswith('project'):
                project_number = item.replace('project', '')
                versions = []
                
                # Check if project has any version folders.
                if os.path.exists(item_path):
                    for version_item in os.listdir(item_path):
                        version_path = os.path.join(item_path, version_item)
                        if os.path.isdir(version_path):
                            versions.append(version_item)
                
                project_info = {
                    'number': project_number,
                    'versions': sorted(versions, key=lambda v: int(v) if v.isdigit() else 0) if versions else []
                }
                projects_info.append(project_info)
                
                # Save project metadata to Redis
                rdb.set(f"project:{project_number}:metadata", json.dumps(project_info))
    
    sorted_projects = sorted(projects_info, key=lambda x: int(x['number']) if x['number'].isdigit() else 0, reverse=True)
    
    # Save projects list to Redis
    rdb.set("projects:list", json.dumps(sorted_projects))
    
    return sorted_projects

def get_next_version_number_for_project(project_number):
    """Get the next version number for a project from Redis."""
    return rdb.incr(f"project:{project_number}:latest_version")

@app.route('/fast/projects', methods=['GET'])
def get_projects():
    return jsonify(get_projects_info())

@app.route('/fast', methods=['GET', 'POST'])
def handle_fast():
    app.logger.info(f"Received {request.method} request to /fast")
    
    if request.method == 'GET':
        return send_file('fast.html')
    
    try:
        data = request.get_json() or request.form.to_dict()
        app.logger.info(f"Data received: {data}")
        
        # Handle project creation
        if data.get('action') == 'create_project':
            project_number = str(data.get('project_number'))
            
            # Update Redis first for a fast response
            rdb.setnx(f"project:{project_number}:latest_version", 0)
            
            # Use a Redis transaction to safely update the highest number
            with rdb.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch("projects:highest_number")
                        current_highest = int(pipe.get("projects:highest_number") or 0)
                        if int(project_number) > current_highest:
                            pipe.multi()
                            pipe.set("projects:highest_number", project_number)
                            pipe.execute()
                        break
                    except redis.WatchError:
                        continue # Retry if another client changed the value

            # Atomically update the projects:list
            with rdb.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch('projects:list')
                        projects_list_json = pipe.get('projects:list')
                        projects = json.loads(projects_list_json) if projects_list_json else []
                        
                        if not any(p['number'] == project_number for p in projects):
                            new_project = {'number': project_number, 'versions': []}
                            projects.append(new_project)
                            projects.sort(key=lambda p: int(p['number']), reverse=True)
                            
                            pipe.multi()
                            pipe.set('projects:list', json.dumps(projects))
                            pipe.execute()
                        break
                    except redis.WatchError:
                        continue
            
            # Offload folder creation to background
            project_path = f"projects/project{project_number}"
            Thread(target=write_to_disk_in_background, args=(f"{project_path}/.placeholder", "")).start()

            app.logger.info(f"Project {project_number} created and list updated in Redis.")
            return jsonify({"status": "created", "project": project_number})
        
        # Handle text submission - save to next version folder as index.html 
        user_id = data.get('key', 'default')
        message = data.get('value', '')
        project_number = data.get('project_number')
        
        if message:  # Only proceed if there's actual content
            if project_number:
                # Use the specified project number
                project_number = str(project_number)
            else:
                # Fall back to highest project if no project specified
                highest_project = get_highest_project_number()
                if highest_project > 0:
                    project_number = str(highest_project)
                else:
                    return jsonify({"error": "No projects found. Create a project first."}), 400
            
            # Get next version number from Redis (very fast)
            next_version_num = get_next_version_number_for_project(project_number)
            version_folder = str(next_version_num)
            
            # Save content to Redis immediately
            redis_key = f"project:{project_number}:{version_folder}:content"
            rdb.set(redis_key, message)
            rdb.set(f"project:{project_number}:{version_folder}:user", user_id)
            rdb.set(f"project:{project_number}:{version_folder}:timestamp", str(int(time.time())))

            # Atomically update the projects:list with the new version
            with rdb.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch('projects:list')
                        projects_list_json = pipe.get('projects:list')
                        if not projects_list_json:
                            app.logger.warning("projects:list is empty, re-running info gathering.")
                            get_projects_info() # This will scan FS and set the list
                            continue # Retry transaction

                        projects = json.loads(projects_list_json)
                        project_updated = False
                        for p in projects:
                            if p['number'] == project_number:
                                if version_folder not in p['versions']:
                                    p['versions'].append(version_folder)
                                    p['versions'].sort(key=lambda v: int(v))
                                project_updated = True
                                break
                        
                        if not project_updated:
                            app.logger.error(f"Project {project_number} not found in projects:list to add a version. Re-syncing.")
                            get_projects_info()
                            continue

                        pipe.multi()
                        pipe.set('projects:list', json.dumps(projects))
                        pipe.execute()
                        break
                    except redis.WatchError:
                        continue

            app.logger.info(f"Saved content to Redis with key: {redis_key} and updated projects:list")
            
            # Offload filesystem writing to a background thread
            index_html_path = f"projects/project{project_number}/{version_folder}/index.html"
            Thread(target=write_to_disk_in_background, args=(index_html_path, message)).start()
            
            return jsonify({
                "status": "saved", 
                "project": project_number,
                "version": version_folder,
                "redis_key": redis_key
            })
        
        return jsonify({"status": "no content"})
        
    except Exception as e:
        app.logger.error(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/<user_id>.html')
def serve_user_page(user_id):
    filename = f"{user_id}.html"
    if os.path.exists(filename):
        return send_file(filename)
    else:
        return "User page not found", 404

@app.route('/projects/<path:filename>')
def serve_project_file(filename):
    return send_file(f"projects/{filename}")

def initialize_redis_from_filesystem():
    """Scan filesystem to initialize/update Redis state on startup."""
    app.logger.info("Initializing Redis state from filesystem...")
    projects_dir = "projects"
    highest_project_num = 0
    if not os.path.exists(projects_dir):
        rdb.setnx("projects:highest_number", 0)
        return

    project_items = [d for d in os.listdir(projects_dir) if os.path.isdir(os.path.join(projects_dir, d))]
    for item in project_items:
        if item.startswith('project'):
            try:
                project_num_str = item.replace('project', '')
                project_num = int(project_num_str)
                highest_project_num = max(highest_project_num, project_num)
                
                project_path = os.path.join(projects_dir, item)
                highest_version = 0
                version_items = [d for d in os.listdir(project_path) if os.path.isdir(os.path.join(project_path, d))]
                for v_item in version_items:
                    if v_item.isdigit():
                        try:
                            version_num = int(v_item)
                            highest_version = max(highest_version, version_num)
                        except ValueError:
                            continue
                rdb.set(f"project:{project_num}:latest_version", highest_version)
            except ValueError:
                continue
    rdb.set("projects:highest_number", highest_project_num)
    app.logger.info(f"Redis initialized. Highest Project: {highest_project_num}")

if __name__ == '__main__': 
    initialize_redis_from_filesystem()
    app.run(debug=True, port=5020) 