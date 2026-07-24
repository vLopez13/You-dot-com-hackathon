/**
 * YouTube Truth Panel — service worker.
 *
 * Thin by design: the side panel talks to the backend and to the content
 * script directly. This worker only opens the panel and makes sure a content
 * script is actually present in the tab the panel is looking at (freshly
 * installed extensions have none until the page reloads).
 */

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});

chrome.runtime.onStartup.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});

async function ensureContentScript(tabId) {
  try {
    const pong = await chrome.tabs.sendMessage(tabId, { type: 'ping' });
    if (pong && pong.ok) return { injected: false, ok: true };
  } catch (_) {
    /* no listener yet — inject below */
  }
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
    return { injected: true, ok: true };
  } catch (e) {
    return { ok: false, error: e && e.message ? e.message : String(e) };
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== 'ensure-content-script') return false;

  const tabId = message.tabId;
  if (typeof tabId !== 'number') {
    sendResponse({ ok: false, error: 'missing tabId' });
    return false;
  }
  ensureContentScript(tabId).then(sendResponse);
  return true;
});

// Let the panel know when the user navigates the tab it is tracking, even on
// full page loads where the content script's own SPA hook does not fire.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== 'complete' || !tab.url) return;
  if (!/^https:\/\/(www\.)?youtube\.com\//.test(tab.url)) return;
  chrome.runtime.sendMessage({ type: 'tab-updated', tabId, url: tab.url }, () => void chrome.runtime.lastError);
});
