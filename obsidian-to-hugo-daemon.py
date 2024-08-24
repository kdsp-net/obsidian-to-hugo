import os
import time
import re
import frontmatter
from slugify import slugify
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import git
from threading import Timer
import shutil
import toml
import logging

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Constants
OBSIDIAN_VAULT_PATH = '/home/kdsp/Documents/Obsidian/Gluon Syndicate'
PUBLIC_RESOURCES_FOLDER = os.path.join(OBSIDIAN_VAULT_PATH, 'Public Resources')
HUGO_REPO_PATH = '/home/kdsp/Documents/gluon-web/gluon-web'
HUGO_CONTENT_PATH = os.path.join(HUGO_REPO_PATH, 'content')
GIT_BRANCH = 'preview'
DEBOUNCE_DELAY = 120  # 2 minutes

# Ensure the Hugo resources folder exists for each language
LANGUAGES = ['english', 'slovak', 'czech']
for lang in LANGUAGES:
    os.makedirs(os.path.join(HUGO_CONTENT_PATH, lang, 'resources'), exist_ok=True)

# Function to convert Obsidian links to Hugo links
def convert_links(content):
    logging.debug(f"Converting links in content: {content[:100]}...")
    content = re.sub(r'\[\[([^\]]+)\]\]', r'[\1](\1)', content)
    return content

# Function to convert front matter to Hugo-compatible format
def convert_front_matter(metadata, original_filename, relative_path):
    logging.debug(f"Converting front matter for: {original_filename}")

    # Use the title from metadata if it exists, otherwise use the filename
    title = metadata.get('title', os.path.splitext(original_filename)[0])

    # Set title and meta_title in Hugo front matter
    metadata['title'] = title
    metadata['meta_title'] = title
    
    # Determine if this is a blog post
    if any(folder.lower() == 'blog' for folder in relative_path.split(os.sep)):
        metadata['type'] = 'blog'
    elif 'type' in metadata:
        del metadata['type']  # Remove the 'type' if it's not a blog post

    toml_front_matter = toml.dumps(metadata)
    return '+++\n' + toml_front_matter + '+++\n'

# Function to create a URL-friendly filename
def url_friendly_filename(filename):
    name, _ = os.path.splitext(filename)
    return slugify(name) + '.md'

# Function to create a URL-friendly folder name
def url_friendly_foldername(foldername):
    return slugify(foldername)

# Function to clean up content
def clean_content(content):
    logging.debug("Cleaning content...")
    # Remove Obsidian ID tags
    content = re.sub(r'\^\w+', '', content)
    # Convert highlight notation to <mark> tags
    content = re.sub(r'==(.*?)==', r'<mark>\1</mark>', content)
    return content

# Function to convert and copy files
def convert_and_copy(filepath, dest_folder, relative_path):
    logging.debug(f"Converting and copying file: {filepath}")
    if os.path.basename(filepath) == '_index.md':
        logging.debug("Skipping _index.md file.")
        return None  # Do not modify _index.md files

    with open(filepath, 'r', encoding='utf-8') as file:
        post = frontmatter.load(file)
        if isinstance(post, str):
            content = post
            metadata = {}
        else:
            content = post.content
            metadata = post.metadata

    content = convert_links(content)
    content = clean_content(content)
    front_matter = convert_front_matter(metadata, os.path.basename(filepath), relative_path)

    hugo_content = front_matter + '\n' + content
    new_filename = url_friendly_filename(os.path.basename(filepath))
    hugo_filepath = os.path.join(dest_folder, new_filename)

    os.makedirs(os.path.dirname(hugo_filepath), exist_ok=True)  # Ensure the directory exists
    with open(hugo_filepath, 'w', encoding='utf-8') as file:
        file.write(hugo_content)

    logging.debug(f"File written to: {hugo_filepath}")
    return hugo_filepath

# Function to create _index.md in new folders
def create_index_file(folder_path, original_folder_name, relative_path):
    index_file_path = os.path.join(folder_path, '_index.md')
    if not os.path.exists(index_file_path):
        logging.debug(f"Creating _index.md in folder: {folder_path}")
        index_content = f"""---
title: "{original_folder_name}"
meta_title: "{original_folder_name}"
"""
        if any(folder.lower() == 'blog' for folder in relative_path.split(os.sep)):
            index_content += 'type: blog\n'

        index_content += '---'
        
        with open(index_file_path, 'w', encoding='utf-8') as file:
            file.write(index_content)

# Git operations
def git_commit_and_push(repo_path, branch, updated_files):
    logging.debug("Committing and pushing changes to Git...")
    repo = git.Repo(repo_path)
    
    # Fetch latest changes
    origin = repo.remote(name='origin')
    origin.fetch()

    # Check if there are changes to pull
    current_branch = repo.head.ref
    if current_branch.tracking_branch().commit != current_branch.commit:
        logging.debug("Pulling latest changes before pushing...")
        repo.git.merge(current_branch.tracking_branch())

    repo.git.add(A=True)
    commit_message = f"Auto-commit from obsidian daemon - Updated files:\n" + "\n".join(updated_files)
    repo.index.commit(commit_message)
    origin.push(branch)

# Debounce mechanism
class DebounceHandler(FileSystemEventHandler):
    def __init__(self):
        self.timer = None
        self.modified_files = set()

    def on_modified(self, event):
        self.handle_event(event)

    def on_created(self, event):
        self.handle_event(event)

    def on_moved(self, event):
        self.handle_event(event)

    def on_deleted(self, event):
        self.handle_event(event)

    def handle_event(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith('.md'):
            logging.debug(f"File event detected: {event.src_path}")
            self.modified_files.add(event.src_path)
            if self.timer:
                self.timer.cancel()
            self.timer = Timer(DEBOUNCE_DELAY, self.process_files)
            self.timer.start()

    def process_files(self):
        logging.debug("Processing modified files...")
        # Sync the entire Public Resources folder
        sync_folders(PUBLIC_RESOURCES_FOLDER, HUGO_CONTENT_PATH)
        git_commit_and_push(HUGO_REPO_PATH, GIT_BRANCH, self.modified_files)
        self.modified_files.clear()

# Function to sync folders recursively
def sync_folders(src, dest):
    logging.debug(f"Syncing folders: {src} -> {dest}")
    src_files_set = set()

    for root, dirs, files in os.walk(src):
        for dir_name in dirs:
            src_dir = os.path.join(root, dir_name)
            relative_dir = os.path.relpath(src_dir, src)

            # Determine the language and resources folder
            lang_folder = relative_dir.split(os.sep)[0]  # This assumes that the language is the top-level directory in Obsidian
            resources_dir = os.path.join(dest, lang_folder, 'resources', url_friendly_foldername(os.path.relpath(src_dir, os.path.join(src, lang_folder))))

            os.makedirs(resources_dir, exist_ok=True)
            create_index_file(resources_dir, dir_name, relative_dir)

        for file_name in files:
            src_file = os.path.join(root, file_name)
            relative_file = os.path.relpath(src_file, src)
            
            # Determine the destination folder for files
            lang_folder = relative_file.split(os.sep)[0]
            dest_folder = os.path.join(dest, lang_folder, 'resources', url_friendly_foldername(os.path.relpath(os.path.dirname(src_file), os.path.join(src, lang_folder))))

            if file_name == '_index.md':
                dest_file = os.path.join(dest_folder, file_name)
                if not os.path.exists(dest_file):
                    shutil.copy2(src_file, dest_file)
            else:
                new_filepath = convert_and_copy(src_file, dest_folder, relative_file)
                if new_filepath:
                    src_files_set.add(new_filepath)

    # Handle deletions
    dest_files = {os.path.join(dp, f) for dp, dn, fn in os.walk(dest) for f in fn}
    files_to_delete = dest_files - src_files_set

    for file_to_delete in files_to_delete:
        if os.path.isfile(file_to_delete) and os.path.basename(file_to_delete) != '_index.md':
            logging.debug(f"Deleting file: {file_to_delete}")
            os.remove(file_to_delete)

# Set up watchdog observer
event_handler = DebounceHandler()
observer = Observer()
observer.schedule(event_handler, PUBLIC_RESOURCES_FOLDER, recursive=True)
observer.start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    observer.stop()
observer.join()
