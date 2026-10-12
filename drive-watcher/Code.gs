/**
 * Google Drive watcher for movie-splitter.
 *
 * Runs inside your Google account (script.google.com), for free, every 5 minutes.
 * If there's a movie (or an archive.org .txt link) in "To edit" and GitHub isn't
 * already busy, it starts the "Make TikTok parts" run. Your PC can stay off.
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
const VIDEO_EXT = /\.(mkv|mp4|avi|mov|m4v|wmv|webm|mpg|mpeg|ts|ogv|txt)$/i;

function checkForNewVideos() {
  if (!hasWaitingFiles()) return;               // nothing to do: costs nothing

  const runs = api('GET', `actions/workflows/${WORKFLOW}/runs?per_page=5`).workflow_runs || [];
  if (runs.some(r => r.status !== 'completed')) {
    // A run is already going. It keeps checking "To edit" after each movie,
    // so it will pick up the new one too (first come, first served).
    return;
  }
  const last = runs[0];
  if (last && last.conclusion === 'failure' &&
      Date.now() - new Date(last.updated_at).getTime() < 60 * 60 * 1000) {
    // Last run broke in the past hour: don't keep retrying every 5 minutes.
    return;
  }
  api('POST', `actions/workflows/${WORKFLOW}/dispatches`, { ref: 'main' });
  console.log('Started a new run.');
}

function hasWaitingFiles() {
  const files = DriveApp.getFolderById(TO_EDIT_FOLDER_ID).getFiles();
  while (files.hasNext()) {
    const f = files.next();
    if (VIDEO_EXT.test(f.getName()) || f.getMimeType().indexOf('video/') === 0) return true;
  }
  return false;
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
