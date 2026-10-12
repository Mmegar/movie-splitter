/**
 * Google Drive watcher for movie-splitter.
 *
 * Runs inside your Google account (script.google.com), for free, every 5 minutes.
 * If there's a movie (or an archive.org .txt link) in any "To edit" folder
 * ("To edit", "To edit (All)", "To edit (Best stretch)") and GitHub isn't
 * already busy, it starts the "Make TikTok parts" run. Your PC can stay off.
 * It also sends a phone notification (ntfy) the first time it sees each new file.
 *
 * Setup (one time):
 *   1. Paste this whole file into a new Apps Script project.
 *   2. Project Settings (gear) > Script properties > add GITHUB_TOKEN = your token.
 *   3. Pick "setUp" in the function menu at the top and press Run. Allow access.
 *   That's it. To stop it later, run "tearDown".
 */

const OWNER = 'Mmegar';
const REPO = 'movie-splitter';
const WORKFLOW = 'make-parts.yml';
const TO_EDIT_FOLDER_ID = '1Mf-bBD8AZofLMk31EQzMlVVlbcDQN5_-';
// Other "To edit" folders next to it (same parent folder), and the mode each one uses.
const FOLDERS = [
  { name: 'To edit', mode: 'Highlights' },
  { name: 'To edit (All)', mode: 'All' },
  { name: 'To edit (Best stretch)', mode: 'Best stretch' },
];
const VIDEO_EXT = /\.(mkv|mp4|avi|mov|m4v|wmv|webm|mpg|mpeg|ts|ogv|txt)$/i;
const IGNORE = ['Finished.txt'];                // the splitter's own list of finished movies
const NTFY_TOPIC = 'kimmeng-movies-b6c4562d7a';  // same as in the GitHub workflow

function checkForNewVideos() {
  const waiting = waitingFiles();
  const newFiles = rememberNew(waiting);
  if (!waiting.length) return;                  // nothing to do: costs nothing

  const runs = api('GET', `actions/workflows/${WORKFLOW}/runs?per_page=5`).workflow_runs || [];
  if (runs.some(r => r.status !== 'completed')) {
    // A run is already going. It keeps checking "To edit" after each movie,
    // so it will pick up the new one too (first come, first served).
    tellPhone(newFiles, 'A movie is already being cut, so this one is in the queue and starts right after it.');
    return;
  }
  const last = runs[0];
  if (last && last.conclusion === 'failure' &&
      Date.now() - new Date(last.updated_at).getTime() < 60 * 60 * 1000) {
    // Last run broke in the past hour: don't keep retrying every 5 minutes.
    tellPhone(newFiles, 'The last run failed less than an hour ago, so it will try again within the hour.');
    return;
  }
  api('POST', `actions/workflows/${WORKFLOW}/dispatches`, { ref: 'main' });
  console.log('Started a new run.');
  tellPhone(newFiles, 'Starting now. You\'ll get a "Started" message once GitHub begins (usually within a minute or two).');
}

function watchedFolders() {
  const main = DriveApp.getFolderById(TO_EDIT_FOLDER_ID);
  const out = [{ folder: main, mode: FOLDERS[0].mode }];
  const parents = main.getParents();
  if (!parents.hasNext()) return out;
  const parent = parents.next();
  FOLDERS.slice(1).forEach(spec => {
    const it = parent.getFoldersByName(spec.name);
    if (it.hasNext()) out.push({ folder: it.next(), mode: spec.mode });
  });
  return out;
}

function waitingFiles() {
  const out = [];
  watchedFolders().forEach(w => {
    const files = w.folder.getFiles();
    while (files.hasNext()) {
      const f = files.next();
      if (IGNORE.indexOf(f.getName()) !== -1) continue;
      if (VIDEO_EXT.test(f.getName()) || f.getMimeType().indexOf('video/') === 0) {
        out.push({ id: f.getId(), name: f.getName(), mode: w.mode });
      }
    }
  });
  return out;
}

/** Returns the files not seen before, and remembers everything currently waiting. */
function rememberNew(waiting) {
  const props = PropertiesService.getScriptProperties();
  const seen = JSON.parse(props.getProperty('SEEN_FILES') || '[]');
  const fresh = waiting.filter(f => seen.indexOf(f.id) === -1);
  props.setProperty('SEEN_FILES', JSON.stringify(waiting.map(f => f.id)));
  return fresh;
}

function tellPhone(files, status) {
  if (!files.length || !NTFY_TOPIC) return;
  const names = files.map(f => `• ${f.name}  (${f.mode} mode)`).join('\n');
  const title = files.length === 1 ? '📥 New file found in "To edit"' : `📥 ${files.length} new files found in "To edit"`;
  try {
    UrlFetchApp.fetch('https://ntfy.sh/', {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ topic: NTFY_TOPIC, title: title, message: names + '\n\n' + status, tags: ['inbox_tray'] }),
      muteHttpExceptions: true,
    });
  } catch (e) {
    console.log('Notification not sent: ' + e);
  }
}

function api(method, path, body) {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Add GITHUB_TOKEN in Project Settings > Script properties.');
  const res = UrlFetchApp.fetch(`https://api.github.com/repos/${OWNER}/${REPO}/${path}`, {
    method: method,
    contentType: 'application/json',
    payload: body ? JSON.stringify(body) : undefined,
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json' },
    muteHttpExceptions: true,
  });
  const code = res.getResponseCode();
  if (code >= 300) throw new Error(`GitHub said ${code}: ${res.getContentText()}`);
  return code === 204 ? {} : JSON.parse(res.getContentText());
}

/** Run once: checks every 5 minutes from now on. */
function setUp() {
  tearDown();
  ScriptApp.newTrigger('checkForNewVideos').timeBased().everyMinutes(5).create();
  checkForNewVideos();
  console.log('Watching "To edit" every 5 minutes.');
}

/** Run to stop watching. */
function tearDown() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
}
