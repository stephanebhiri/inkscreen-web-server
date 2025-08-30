let currentFolder = '';
let selectedImage = null;
let draggedElement = null;
let statusTimer = null;
let countdownTimer = null;
let notificationTimeout = null;

// --- UI Update Functions ---

function showNotification(title, message, type = 'success', progress = null) {
    const notification = document.getElementById('notification');
    const icon = document.getElementById('notificationIcon');
    const titleEl = document.getElementById('notificationTitle');
    const messageEl = document.getElementById('notificationMessage');
    const progressEl = document.getElementById('notificationProgress');
    const progressFill = document.getElementById('notificationProgressFill');
    
    if (notificationTimeout) clearTimeout(notificationTimeout);
    
    titleEl.textContent = title;
    messageEl.textContent = message;
    
    progressEl.style.display = progress !== null ? 'block' : 'none';
    if (progress !== null) {
        progressFill.style.width = progress + '%';
        progressFill.textContent = Math.round(progress) + '%';
    }
    
    notification.className = `notification ${type} show`;
    icon.textContent = {'success': '✓', 'error': '✗', 'info': 'ℹ', 'progress': '⏳'}[type];
    
    if (type !== 'progress') {
        notificationTimeout = setTimeout(() => notification.classList.remove('show'), 3000);
    }
}

function hideNotification() {
    const notification = document.getElementById('notification');
    notification.classList.remove('show');
    if (notificationTimeout) clearTimeout(notificationTimeout);
}

function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('open');
    document.getElementById('hamburger').classList.toggle('active');
}

// --- ESP32 Stats ---

function updateEsp32Stats() {
    fetch('/api/esp32/stats')
        .then(r => r.json())
        .then(stats => {
            const battery = document.getElementById('esp32Battery');
            const wifi = document.getElementById('esp32WiFi');
            
            if (stats.battery >= 0) {
                let batteryIcon = stats.battery > 20 ? '🔋' : '🪫';
                battery.innerHTML = `${batteryIcon} ${stats.battery}%`;
            } else {
                battery.innerHTML = '🔌 USB';
            }
            
            if (stats.rssi) {
                let signal = stats.rssi > -50 ? '📶' : stats.rssi > -70 ? '📶' : '📶';
                wifi.innerHTML = `${signal} ${stats.rssi}dBm`;
            } else {
                wifi.innerHTML = '📶 -';
            }
        })
        .catch(() => {
            document.getElementById('esp32Battery').innerHTML = '-';
            document.getElementById('esp32WiFi').innerHTML = '-';
        });
}

// --- Health Check ---
function updateHealth() {
    fetch('/healthz')
        .then(r => r.json())
        .then(h => {
            const dot = document.getElementById('healthIndicator');
            dot.classList.remove('health-ok', 'health-bad', 'health-warn');
            if (h.ok) {
                dot.classList.add('health-ok');
                dot.title = 'OK';
            } else {
                dot.classList.add('health-bad');
                dot.title = h.error || 'Not OK';
            }
        })
        .catch(() => {
            const dot = document.getElementById('healthIndicator');
            dot.classList.remove('health-ok', 'health-warn');
            dot.classList.add('health-bad');
            dot.title = 'Unavailable';
        });
}

// --- Cleanup Stats ---
function updateCleanupStats() {
    fetch('/api/thumbnails/cleanup_stats')
        .then(r => r.json())
        .then(stats => {
            const el = document.getElementById('dynamicCleanedCount');
            if (!el) return;
            const val = typeof stats.dynamic_cleaned === 'number' ? stats.dynamic_cleaned : '-';
            el.textContent = val;
            if (stats.last_run) {
                el.title = `Last run: ${stats.last_run}`;
            }
        })
        .catch(() => {
            const el = document.getElementById('dynamicCleanedCount');
            if (el) el.textContent = '-';
        });
}

// --- Slideshow and Status ---

function startStatusPolling() {
    updateStatus();
    statusTimer = setInterval(updateStatus, 2000);
}

function updateStatus() {
    fetch('/api/slideshow/status')
        .then(r => r.json())
        .then(status => {
            updateSlideshowStatusUI(status);
            updateNowPlayingUI(status);
        });
    updateEsp32Stats();
    updateHealth();
    updateCleanupStats();
}

function updateSlideshowStatusUI(status) {
    const indicator = document.getElementById('statusIndicator');
    const statusText = document.getElementById('statusText');
    const playBtn = document.getElementById('playBtn');
    const stopBtn = document.getElementById('stopBtn');
    const progress = document.getElementById('playlistProgress');
    const nextChange = document.getElementById('nextChange');
    const loopStatus = document.getElementById('loopStatus');
    const shuffleStatus = document.getElementById('shuffleStatus');

    if (status.running) {
        indicator.classList.add('active');
        statusText.textContent = 'Playing';
        playBtn.style.display = 'none';
        stopBtn.style.display = 'inline-block';
        
        if (status.total_images > 0) {
            progress.textContent = status.loop_enabled 
                ? `${status.current_index}/${status.total_images} (Loop ${status.loop_count + 1})` 
                : `${status.current_index}/${status.total_images}`;
        } else {
            progress.textContent = '-';
        }

        if (status.next_change) {
            const remaining = Math.max(0, Math.floor(status.next_change - Date.now() / 1000));
            const minutes = Math.floor(remaining / 60);
            const seconds = remaining % 60;
            nextChange.textContent = remaining > 0 ? `${minutes}:${seconds.toString().padStart(2, '0')}` : '0:00';
        } else {
            nextChange.textContent = '-';
        }
        
        loopStatus.textContent = status.loop_enabled ? 'On' : 'Off';
        shuffleStatus.textContent = status.shuffle_enabled ? 'On' : 'Off';
    } else {
        indicator.classList.remove('active');
        statusText.textContent = 'Idle';
        playBtn.style.display = 'inline-block';
        stopBtn.style.display = 'none';
        progress.textContent = '-';
        nextChange.textContent = '-';
        loopStatus.textContent = 'Off';
        shuffleStatus.textContent = 'Off';
    }
}

function updateNowPlayingUI(status) {
    const nowPlaying = document.getElementById('nowPlaying');
    nowPlaying.classList.toggle('active', status.running);

    if (status.running) {
        document.getElementById('playingFolder').textContent = status.current_folder || 'Root';
        
        const currentThumbnail = document.getElementById('currentThumbnail');
        const currentImageName = document.getElementById('currentImageName');
        if (status.current_image) {
            const currentPath = status.current_folder ? `${status.current_folder}/${status.current_image}` : status.current_image;
            currentThumbnail.loading = 'lazy';
            currentThumbnail.src = `/api/thumbnail/${encodeURIComponent(currentPath)}?w=200&q=85`;
            currentImageName.textContent = status.current_image;
            currentThumbnail.onclick = () => pushImageDirect(currentPath);
        }

        const nextThumbnail = document.getElementById('nextThumbnail');
        const nextImageName = document.getElementById('nextImageName');
        if (status.next_image) {
            const nextPath = status.current_folder ? `${status.current_folder}/${status.next_image}` : status.next_image;
            nextThumbnail.loading = 'lazy';
            nextThumbnail.src = `/api/thumbnail/${encodeURIComponent(nextPath)}?w=200&q=85`;
            nextImageName.textContent = status.next_image;
            nextThumbnail.onclick = () => pushImageDirect(nextPath);
        } else {
            nextThumbnail.src = '';
            nextImageName.textContent = 'End of playlist';
        }
    }
}

// --- Drag and Drop ---

function setupDragAndDrop() {
    const items = document.querySelectorAll('.image-item');
    items.forEach(item => {
        item.addEventListener('dragstart', handleDragStart);
        item.addEventListener('dragend', handleDragEnd);
        item.addEventListener('dragover', handleDragOver);
        item.addEventListener('dragleave', handleDragLeave);
        item.addEventListener('drop', handleDrop);
        // Touch events for mobile are not refactored yet for simplicity
        // They can be refactored in a similar way
    });
}

function handleDragStart(e) {
    draggedElement = this;
    this.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
}

function handleDragEnd(e) {
    this.classList.remove('dragging');
    document.querySelectorAll('.image-item').forEach(item => 
        item.classList.remove('drag-over', 'drag-over-left', 'drag-over-right')
    );
    draggedElement = null;
}

function handleDragOver(e) {
    e.preventDefault();
    if (draggedElement && draggedElement !== this) {
        this.classList.remove('drag-over', 'drag-over-left', 'drag-over-right');
        const rect = this.getBoundingClientRect();
        const middle = rect.left + (rect.width / 2);
        this.classList.add(e.clientX < middle ? 'drag-over-left' : 'drag-over-right');
    }
}

function handleDragLeave(e) {
    this.classList.remove('drag-over', 'drag-over-left', 'drag-over-right');
}

function handleDrop(e) {
    e.preventDefault();
    this.classList.remove('drag-over', 'drag-over-left', 'drag-over-right');
    if (draggedElement && draggedElement !== this) {
        const rect = this.getBoundingClientRect();
        const middle = rect.left + (rect.width / 2);
        if (e.clientX < middle) {
            this.parentNode.insertBefore(draggedElement, this);
        } else {
            this.parentNode.insertBefore(draggedElement, this.nextSibling);
        }
        updatePlaylistOrder();
    }
}


// --- API Calls ---

function toggleSlideshow() {
    showNotification('Starting Slideshow', `Playing from ${currentFolder || 'Root'}...`, 'info');
    fetch(`/api/slideshow/start/${encodeURIComponent(currentFolder)}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showNotification('Slideshow Started', `Now playing from ${currentFolder || 'Root'}`, 'success');
                updateStatus();
            } else {
                showNotification('Start Failed', 'Unable to start slideshow', 'error');
            }
        });
}

function stopSlideshow() {
    fetch('/api/slideshow/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showNotification('Slideshow Stopped', 'Playback has been stopped', 'success');
                updateStatus();
            }
        });
}

// ... other API call functions ...

// --- Initialization ---

document.addEventListener('DOMContentLoaded', function() {
    loadFolderTree();
    loadFolder('');
    startStatusPolling();
    setupMobileInteractions();
    setupUploadZone();
});

function setupMobileInteractions() {
    const sidebar = document.getElementById('sidebar');
    const hamburger = document.getElementById('hamburger');
    
    // Close sidebar when clicking outside
    document.addEventListener('click', function(e) {
        if (window.innerWidth <= 768) {
            if (!sidebar.contains(e.target) && !hamburger.contains(e.target)) {
                sidebar.classList.remove('open');
                hamburger.classList.remove('active');
            }
        }
    });
    
    // Hamburger click handler
    hamburger.addEventListener('click', function(e) {
        e.stopPropagation();
        toggleSidebar();
    });
    
    // Auto-close via event delegation so it works after re-render
    sidebar.addEventListener('click', function(e) {
        const itemContent = e.target.closest('.folder-item-content');
        if (itemContent && window.innerWidth <= 768) {
            setTimeout(() => {
                sidebar.classList.remove('open');
                hamburger.classList.remove('active');
            }, 150);
        }
    });
    updateEsp32Stats();
}

function setupUploadZone() {
    const fileInput = document.getElementById('fileInput');
    const dropOverlay = document.getElementById('dropOverlay');
    const dropPill = document.getElementById('dropPill');
    let dragDepth = 0;
    
    fileInput.addEventListener('change', (e) => handleFiles(e.target.files));

    // Global overlay for drag & drop anywhere
    window.addEventListener('dragenter', (e) => {
        dragDepth++;
        if (e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files')) {
            dropOverlay.style.display = 'flex';
            if (dropPill) dropPill.classList.add('dragover');
        }
    });
    window.addEventListener('dragleave', () => {
        dragDepth = Math.max(0, dragDepth - 1);
        if (dragDepth === 0) {
            dropOverlay.style.display = 'none';
            if (dropPill) dropPill.classList.remove('dragover');
        }
    });
    window.addEventListener('dragover', (e) => {
        e.preventDefault();
    });
    window.addEventListener('drop', (e) => {
        e.preventDefault();
        dropOverlay.style.display = 'none';
        dragDepth = 0;
        if (dropPill) dropPill.classList.remove('dragover');
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
            handleFiles(e.dataTransfer.files);
        }
    });

    if (dropPill) {
        ['dragover','dragleave','drop'].forEach(evt => {
            dropPill.addEventListener(evt, (e) => {
                e.preventDefault();
                e.stopPropagation();
                if (evt === 'dragover') dropPill.classList.add('dragover');
                if (evt === 'dragleave' || evt === 'drop') dropPill.classList.remove('dragover');
            });
        });
        dropPill.addEventListener('drop', (e) => {
            if (e.dataTransfer && e.dataTransfer.files) {
                handleFiles(e.dataTransfer.files);
            }
        });
    }
}

function loadFolderTree() {
    fetch('/api/folders')
        .then(r => r.json())
        .then(data => {
            const tree = document.getElementById('folderTree');
            tree.innerHTML = renderFolderTree(data.tree);
        });
    updateEsp32Stats();
}

function renderFolderTree(items, level = 0) {
    let html = '';
    for (const item of items) {
        const isRoot = item.name === '📁 Root';
        const displayName = isRoot ? 'Home' : item.name;
        
        html += `
            <div class="folder-item ${currentFolder === item.path ? 'active' : ''}" 
                 data-folder-path="${item.path}"
                 draggable="true" 
                 ondragstart="startFolderDrag(event)"
                 ondragover="handleFolderDragOver(event)"
                 ondrop="handleFolderDrop(event)">
                <div class="folder-item-content" onclick="loadFolder('${item.path}')" style="margin-left: ${level * 16}px">
                    <div class="folder-left">
                        <span class="folder-icon">${isRoot ? '🏠' : (level > 0 ? '📁' : '🗂️')}</span>
                        <span class="folder-name">${displayName}</span>
                    </div>
                    <div class="folder-actions">
                        <span class="folder-badge">${item.image_count}</span>
                        ${item.path !== '' ? `<button class="folder-action-btn rename" onclick="startRename(event, '${item.path}')" title="Rename folder">✏️</button>` : ''}
                        ${item.path !== '' ? `<button class="folder-action-btn delete" onclick="event.stopPropagation(); deleteFolder('${item.path}')" title="Delete folder">×</button>` : ''}
                    </div>
                </div>
            </div>`;
        
        if (item.children && item.children.length > 0) {
            html += renderFolderTree(item.children, level + 1);
        }
    }
    return html;
}

function loadFolder(path) {
    currentFolder = path;
    
    // Update breadcrumb
    const parts = path.split('/').filter(p => p);
    let breadcrumb = '<a href="#" class="breadcrumb-item" onclick="loadFolder(\'\')">Home</a>';
    let currentPath = '';
    for (const part of parts) {
        currentPath += (currentPath ? '/' : '') + part;
        breadcrumb += ` > <a href="#" class="breadcrumb-item" onclick="loadFolder('${currentPath}')">${part}</a>`;
    }
    document.getElementById('breadcrumb').innerHTML = breadcrumb;
    
    
    // Load images
    fetch(`/api/playlist/${encodeURIComponent(path)}`)
        .then(r => r.json())
        .then(data => {
            renderImages(data.images, data.playlist);
        });
    updateEsp32Stats();
    
    // Refresh folder tree
    loadFolderTree();
}

function renderImages(images, playlist) {
    const grid = document.getElementById('imageGrid');
    const order = playlist.order || [];
    
    // Sort images by order
    const sortedImages = [...images].sort((a, b) => {
        const aIndex = order.indexOf(a.name);
        const bIndex = order.indexOf(b.name);
        if (aIndex === -1 && bIndex === -1) return 0;
        if (aIndex === -1) return 1;
        if (bIndex === -1) return -1;
        return aIndex - bIndex;
    });
    
    if (sortedImages.length === 0) {
        grid.innerHTML = `
            <div style="text-align:center; padding: 30px; color:#64748b;">
                <div style="font-size:40px;">🗂️</div>
                <div style="margin-top:8px; font-weight:600;">This folder is empty</div>
                <div style="margin-top:6px;">Click <strong>Upload Images</strong> or drop files anywhere</div>
            </div>`;
        return;
    }
    grid.innerHTML = sortedImages.map((img, index) => `
        <div class="image-item" draggable="true" data-image="${img.name}" data-index="${index}">
            <div class="order-badge">#${index + 1}</div>
            <img loading="lazy" src="/api/thumbnail/${encodeURIComponent((currentFolder ? currentFolder + '/' : '') + img.name)}?w=300&q=80" alt="${img.name}">
            <div class="image-info">
                <div class="image-name" title="${img.name}">${img.name}</div>
                <div class="image-actions">
                    <button class="btn btn-primary" onclick="setCurrentImage('${img.name}')">Set Current</button>
                    <button class="btn btn-secondary" onclick="showMoveModal('${img.name}')">Move</button>
                    <button class="btn btn-danger" onclick="deleteImage('${img.name}')">Delete</button>
                </div>
            </div>
        </div>
    `).join('');
    
    // Setup drag and drop
    setupDragAndDrop();
}

function updatePlaylistOrder() {
    const items = document.querySelectorAll('.image-item');
    const newOrder = Array.from(items).map(item => item.dataset.image);
    
    fetch(`/api/playlist/${encodeURIComponent(currentFolder)}/order`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({order: newOrder})
    }).then(() => {
        loadFolder(currentFolder);
    });
}

function handleFiles(files) {
    const formData = new FormData();
    let fileCount = 0;
    
    for (const file of files) {
        if (file.type.startsWith('image/')) {
            formData.append('files', file);
            fileCount++;
        }
    }
    
    if (fileCount === 0) return;
    
    // Show progress
    const progressBar = document.getElementById('uploadProgress');
    const progressFill = document.getElementById('uploadProgressFill');
    progressBar.style.display = 'block';
    
    const xhr = new XMLHttpRequest();
    
    xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
            const percent = Math.round((e.loaded / e.total) * 100);
            progressFill.style.width = percent + '%';
            progressFill.textContent = percent + '%';
        }
    });
    
    xhr.addEventListener('load', () => {
        progressBar.style.display = 'none';
        if (xhr.status === 200) {
            const response = JSON.parse(xhr.responseText);
            showNotification('Upload Complete', `${response.uploaded.length} images uploaded`, 'success');
        } else {
            showNotification('Upload Failed', 'Error uploading images', 'error');
        }
        loadFolder(currentFolder);
    });
    
    xhr.open('POST', `/api/upload/${encodeURIComponent(currentFolder)}`);
    xhr.send(formData);
}

function trackPushProgress(jobId, imageName) {
    const checkStatus = () => {
        fetch(`/api/push/status/${jobId}`)
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    showNotification('Push Error', 'Job not found', 'error');
                    return;
                }
                
                const { status, progress, message } = data;
                
                if (status === 'completed') {
                    showNotification('Push Complete!', `${imageName} sent successfully`, 'success');
                } else if (status === 'failed') {
                    showNotification('Push Failed', data.error || message, 'error');
                } else {
                    // Still in progress
                    showNotification('Pushing Image', message, 'progress', progress);
                    setTimeout(checkStatus, 500); // Poll every 500ms
                }
            })
            .catch(err => {
                showNotification('Push Error', 'Lost connection to server', 'error');
            });
    };
    
    checkStatus();
}

function setCurrentImage(imageName) {
    const imagePath = currentFolder ? currentFolder + '/' + imageName : imageName;
    fetch(`/api/set_current`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image_path: imagePath })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showNotification('Current Image Set', `${imageName} will display on next ESP32 poll`, 'success');
            updateStatus();
            setTimeout(updateStatus, 500);
            setTimeout(updateStatus, 1500);
        } else {
            showNotification('Failed', data.error || 'Unable to set current image', 'error');
        }
    })
    .catch(err => {
        showNotification('Error', 'Failed to communicate with server', 'error');
    });
}

function pushImageDirect(imagePath) {
    const imageName = imagePath.split('/').pop();
    fetch(`/api/push/${encodeURIComponent(imagePath)}`, {
        method: 'POST'
    })
    .then(r => r.json())
    .then(data => {
        if (data.success && data.job_id) {
            trackPushProgress(data.job_id, imageName);
        } else {
            showNotification('Push Failed', data.error || 'Unable to start push', 'error');
        }
    })
    .catch(err => {
        showNotification('Push Error', 'Failed to communicate with server', 'error');
    });
}

function pushNextNow() {
    showNotification('Skipping to Next', 'Pushing next image...', 'info');
    fetch('/api/slideshow/next', {
        method: 'POST'
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showNotification('Next Image Pushed!', data.pushed || 'Skipped to next image', 'success');
            setTimeout(updateStatus, 500);
            setTimeout(updateStatus, 1500);
        } else {
            showNotification('Skip Failed', data.error || 'Unable to skip', 'error');
        }
    })
    .catch(err => {
        showNotification('Skip Error', 'Failed to skip to next', 'error');
    });
}

function deleteImage(imageName) {
    if (confirm(`Delete ${imageName}?`)) {
        const path = (currentFolder ? currentFolder + '/' : '') + imageName;
        fetch(`/api/image/${encodeURIComponent(path)}`, {
            method: 'DELETE'
        }).then(() => {
            loadFolder(currentFolder);
        });
    }
    updateEsp32Stats();
}

function refreshThumbnails(folderPath) {
    showNotification('Refreshing thumbnails...', 'Regenerating thumbnails…', 'progress');
    
    const url = folderPath 
        ? `/api/thumbnails/refresh/${encodeURIComponent(folderPath)}` 
        : '/api/thumbnails/refresh';
        
    fetch(url, {
        method: 'POST'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showNotification('Thumbnails Refreshed', `✅ Regenerated ${data.regenerated} thumbnails`, 'success');
            // Reload current folder to show new thumbnails
            loadFolder(currentFolder);
        } else {
            showNotification('Failed to refresh thumbnails', 'error');
        }
    })
    .catch(error => {
        showNotification('Error refreshing thumbnails', 'error');
        console.error('Refresh error:', error);
    });
}

function startRename(event, folderPath) {
    event.stopPropagation();
    event.preventDefault();
    console.log('Renaming folder:', folderPath);
    const folderItem = document.querySelector(`[data-folder-path="${folderPath}"] .folder-name`);
    const currentName = folderItem.textContent;
    
    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentName;
    input.className = 'folder-rename-input';
    input.style.cssText = 'background: white; border: 1px solid #3b82f6; border-radius: 4px; padding: 2px 6px; font-size: 14px; font-weight: 500; width: 120px;';
    
    folderItem.replaceWith(input);
    input.focus();
    input.select();
    
    // Prevent folder selection when clicking in input
    input.addEventListener('click', (e) => {
        e.stopPropagation();
    });
    
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            const newName = input.value.trim();
            if (newName && newName !== currentName) {
                fetch(`/api/folder/${encodeURIComponent(folderPath)}/rename`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({new_name: newName})
                }).then(response => response.json()).then(data => {
                    if (data.success) {
                        loadFolderTree();
                    } else {
                        alert('Failed to rename folder: ' + (data.error || 'Unknown error'));
                    }
                });
            } else {
                folderItem.textContent = currentName;
                input.replaceWith(folderItem);
            }
        }
        if (e.key === 'Escape') {
            folderItem.textContent = currentName;
            input.replaceWith(folderItem);
        }
    });
}

function deleteFolder(folderPath) {
    if (confirm(`Delete folder "${folderPath}" and all its contents?`)) {
        fetch(`/api/folder/${encodeURIComponent(folderPath)}`, {
            method: 'DELETE'
        }).then(response => {
            if (response.ok) {
                // Navigate to parent folder
                const parentPath = folderPath.split('/').slice(0, -1).join('/');
                loadFolder(parentPath);
            } else {
                alert('Failed to delete folder');
            }
        });
    }
    updateEsp32Stats();
}

let draggedFolder = null;

function startFolderDrag(event) {
    draggedFolder = event.target.closest('.folder-item');
    draggedFolder.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
}

function handleFolderDragOver(event) {
    event.preventDefault();
    if (draggedFolder && draggedFolder !== event.target.closest('.folder-item')) {
        event.target.closest('.folder-item').classList.add('drag-over');
    }
}

function handleFolderDrop(event) {
    event.preventDefault();
    const targetFolder = event.target.closest('.folder-item');
    targetFolder.classList.remove('drag-over');
    
    if (draggedFolder && draggedFolder !== targetFolder) {
        const sourcePath = draggedFolder.getAttribute('data-folder-path');
        const targetPath = targetFolder.getAttribute('data-folder-path');
        
        if (sourcePath && sourcePath !== targetPath) {
            moveFolder(sourcePath, targetPath);
        }
    }
    
    draggedFolder.classList.remove('dragging');
    draggedFolder = null;
}

function moveFolder(sourcePath, targetPath) {
    const sourceName = sourcePath.split('/').pop();
    const newPath = targetPath ? `${targetPath}/${sourceName}` : sourceName;
    
    fetch(`/api/folder/move`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            source: sourcePath,
            target: newPath
        })
    }).then(response => {
        if (response.ok) {
            loadFolderTree();
            loadFolder(currentFolder);
        } else {
            alert('Failed to move folder');
        }
    });
}

function showNewFolderModal() {
    document.getElementById('newFolderModal').classList.add('show');
}

function createNewFolder() {
    const name = document.getElementById('newFolderName').value.trim();
    if (!name) return;
    
    const path = currentFolder ? `${currentFolder}/${name}` : name;
    
    fetch('/api/folder', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: path})
    }).then(() => {
        closeModal('newFolderModal');
        document.getElementById('newFolderName').value = '';
        loadFolderTree();
        loadFolder(path);
    });
}

function showPlaylistSettings() {
    fetch(`/api/playlist/${encodeURIComponent(currentFolder)}`)
        .then(r => r.json())
        .then(data => {
            const settings = data.playlist.settings;
            document.getElementById('intervalInput').value = settings.interval;
            document.getElementById('loopToggle').classList.toggle('active', settings.loop);
            document.getElementById('shuffleToggle').classList.toggle('active', settings.shuffle);
            document.getElementById('descriptionInput').value = data.playlist.description || '';
            document.getElementById('settingsModal').classList.add('show');
        });
    updateEsp32Stats();
}

function savePlaylistSettings() {
    const settings = {
        interval: parseInt(document.getElementById('intervalInput').value),
        loop: document.getElementById('loopToggle').classList.contains('active'),
        shuffle: document.getElementById('shuffleToggle').classList.contains('active')
    };
    
    const description = document.getElementById('descriptionInput').value;
    
    fetch(`/api/playlist/${encodeURIComponent(currentFolder)}/settings`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({settings, description})
    }).then(() => {
        closeModal('settingsModal');
        loadFolder(currentFolder);
    });
}

function showMoveModal(imageName) {
    selectedImage = imageName;
    
    // Load folders for destination
    fetch('/api/folders')
        .then(r => r.json())
        .then(data => {
            const select = document.getElementById('destinationFolder');
            select.innerHTML = '<option value="">Root</option>';
            
            function addFolders(items, prefix = '') {
                for (const item of items) {
                    if (item.path !== currentFolder) {
                        select.innerHTML += `<option value="${item.path}">${prefix}${item.name}</option>`;
                    }
                    if (item.children) {
                        addFolders(item.children, prefix + '  ');
                    }
                }
            }
            
            addFolders(data.tree);
            document.getElementById('moveModal').classList.add('show');
        });
    updateEsp32Stats();
}

function confirmMove() {
    const destination = document.getElementById('destinationFolder').value;
    
    fetch('/api/move', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            image: selectedImage,
            from: currentFolder,
            to: destination
        })
    }).then(() => {
        closeModal('moveModal');
        loadFolder(currentFolder);
    });
}

function toggleSwitch(element) {
    element.classList.toggle('active');
}

function closeModal(modalId) {
    document.getElementById(modalId).classList.remove('show');
}

function refreshStatus() {
    loadFolder(currentFolder);
    updateStatus();
    setTimeout(updateStatus, 500);
    setTimeout(updateStatus, 1500);
}
