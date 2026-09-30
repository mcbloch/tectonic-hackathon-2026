/* Teams Copilot demo: dependency-free mock of Teams + Copilot for the SD Worx workspace.
   Copilot answers are real model output: the question goes to /api/chat (teams/server.py),
   which sends the whole mock-data pack as context. Files and Mail are read-only mock surfaces. */

const VIEW_TABS = [
  { id: "copilot", label: "Copilot", icon: "copilot" },
  { id: "files", label: "Files", icon: "file" },
  { id: "mail", label: "Mail", icon: "mail" },
];

const APP_RAIL_ITEMS = [
  { name: "teams", label: "Teams", current: true },
  { name: "activity", label: "Activity" },
  { name: "chat", label: "Chat" },
  { name: "calendar", label: "Calendar" },
  { name: "calls", label: "Calls" },
  { name: "copilot", label: "Copilot" },
];

const MOCK_TEAMS = [
  {
    id: "sd-worx",
    name: "SD Worx",
    channels: [
      { id: "general", name: "General" },
      { id: "shared", name: "Shared" },
      { id: "onboarding", name: "Onboarding" },
    ],
  },
];

let MOCK_FILES = [
  { name: "Payroll Calendar.xlsx", type: "XLSX", size: "42 KB", modified: "Today, 09:24", owner: "Mihaly Csonka" },
  { name: "Client Overview.docx", type: "DOCX", size: "186 KB", modified: "Yesterday", owner: "Rostyslav Fedorov" },
  { name: "Onboarding Checklist.pdf", type: "PDF", size: "1.2 MB", modified: "Sep 29", owner: "Maxime Bloch" },
  { name: "Team Contacts.xlsx", type: "XLSX", size: "28 KB", modified: "Sep 27", owner: "Robert Akhmerov" },
];

let MOCK_MAIL = [
  {
    sender: "Sarah De Smet",
    subject: "Payroll coordination for October",
    preview: "Can we confirm the cut-off and approvers before Friday?",
    time: "09:16",
  },
  {
    sender: "SD Worx Client Team",
    subject: "Welcome pack updates",
    preview: "The latest onboarding notes are ready for review.",
    time: "Yesterday",
  },
  {
    sender: "Finance Operations",
    subject: "Timesheet reminder",
    preview: "Please review outstanding timesheets before payroll close.",
    time: "Sep 29",
  },
];

const MAIL_FOLDERS = [
  { name: "Inbox", icon: "mail" },
  { name: "Sent", icon: "send" },
  { name: "Drafts", icon: "file" },
];

const MAIL_READING_BODY =
  "Hi team, please confirm the October payroll cut-off and approvers before Friday. I\u2019ve linked the shared calendar for context.";

const COPILOT_PROMPTS = [
  "What should I know about this client?",
  "What is the payroll deadline?",
  "Summarize the shared files",
];

const state = {
  activeView: "copilot",
  selectedTeamId: "sd-worx",
  selectedChannelId: "shared",
  pending: false,
  messages: [
    {
      id: "welcome",
      role: "assistant",
      text: "I\u2019m Copilot for the Proximus Belgium account. I have read this workspace: Teams channels, Outlook, SharePoint and the client memory. Ask me about cut-off rules, exceptions, owners or open issues.",
    },
  ],
};

let messageSeq = 0;

/* ---------- small helpers ---------- */

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function esc(value) {
  return String(value).replace(/[&<>"']/g, (character) => HTML_ESCAPES[character]);
}

const ICON_PATHS = {
  activity:
    '<path d="M12 4.2a4.3 4.3 0 0 0-4.3 4.3v3.1L6.3 14.2h11.4l-1.4-2.6V8.5A4.3 4.3 0 0 0 12 4.2Z"/><path d="M10.1 16.6a2 2 0 0 0 3.8 0"/>',
  chat: '<path d="M5.1 5.6h13.8v9.1h-7.7L7.1 18v-3.3H5.1Z"/>',
  teams:
    '<circle cx="9.4" cy="8.4" r="2.7"/><path d="M4.4 18.3c0-2.6 2.1-4 5-4s5 1.4 5 4"/><path d="M16.1 6.2a2.4 2.4 0 0 1 0 4.6"/><path d="M17.4 14.5c1.5.5 2.5 1.6 2.5 3.2"/>',
  calendar:
    '<rect x="4.4" y="6" width="15.2" height="13.6" rx="2"/><path d="M4.4 10.2h15.2"/><path d="M9 4.4v3.4"/><path d="M15 4.4v3.4"/>',
  calls:
    '<path d="M6.6 4.8h3l1.4 3.4-1.8 1.5a10.4 10.4 0 0 0 4.8 4.8l1.5-1.8 3.4 1.4v3a1.6 1.6 0 0 1-1.7 1.6C10.9 18.1 5.6 12.8 5 6.5a1.6 1.6 0 0 1 1.6-1.7Z"/>',
  copilot:
    '<path d="M10.6 4.4 12 8.3l3.9 1.4-3.9 1.4-1.4 3.9-1.4-3.9L5.3 9.7l3.9-1.4Z"/><path d="M17.6 14.8l.7 1.9 1.9.7-1.9.7-.7 1.9-.7-1.9-1.9-.7 1.9-.7Z"/>',
  folder:
    '<path d="M4.4 7.2a1.8 1.8 0 0 1 1.8-1.8h3.2l1.8 2.1h6.6a1.8 1.8 0 0 1 1.8 1.8v7a1.8 1.8 0 0 1-1.8 1.8H6.2a1.8 1.8 0 0 1-1.8-1.8Z"/>',
  file: '<path d="M7.2 3.8h5.9l4.7 4.7v11.7H7.2Z"/><path d="M12.9 3.8v4.9h4.9"/>',
  mail: '<rect x="3.8" y="6.2" width="16.4" height="11.6" rx="2"/><path d="M4.7 7.4 12 12.7l7.3-5.3"/>',
  send: '<path d="M4.6 11.6 19.4 4.6l-7 14.8-1.6-6.2Z"/><path d="M10.8 13.2 19.4 4.6"/>',
};

function icon(name, label) {
  const a11y = label ? `role="img" aria-label="${esc(label)}"` : 'aria-hidden="true"';
  return `<svg class="icon icon-${name}" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ${a11y} focusable="false">${ICON_PATHS[name] ?? ""}</svg>`;
}

function currentTeam() {
  return MOCK_TEAMS.find((team) => team.id === state.selectedTeamId) ?? MOCK_TEAMS[0];
}

function currentChannel() {
  const team = currentTeam();
  return team.channels.find((channel) => channel.id === state.selectedChannelId) ?? team.channels[0];
}

function initials(name) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => word[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

/* ---------- navigation rail and sidebar ---------- */

function renderAppRail() {
  return `<nav class="rail" aria-label="Applications">
    <ul class="rail-list">
      ${APP_RAIL_ITEMS.map(
        (item) => `<li class="rail-item${item.current ? " is-current" : ""}"${item.current ? ' aria-current="page"' : ""}>
        ${icon(item.name)}
        <span class="sr-only">${esc(item.label)}</span>
      </li>`,
      ).join("")}
    </ul>
  </nav>`;
}

function renderTeamSidebar() {
  const team = currentTeam();
  return `<aside class="sidebar" aria-label="Teams and channels">
    <div class="sidebar-head">
      <span class="team-badge" aria-hidden="true">${esc(initials(team.name))}</span>
      <h2 class="team-name">${esc(team.name)}</h2>
    </div>
    <p class="sidebar-section">Channels</p>
    <ul class="channel-list">
      ${team.channels
        .map((channel) => {
          const selected = channel.id === state.selectedChannelId;
          return `<li class="channel${selected ? " is-selected" : ""}"${selected ? ' aria-current="location"' : ""}>
        <span class="channel-hash" aria-hidden="true">#</span>
        <span class="channel-name">${esc(channel.name)}</span>
      </li>`;
        })
        .join("")}
    </ul>
  </aside>`;
}

/* ---------- workspace header ---------- */

function renderWorkspaceHeader() {
  const tab = VIEW_TABS.find((entry) => entry.id === state.activeView) ?? VIEW_TABS[0];
  const crumbs = ["Teams", currentTeam().name, currentChannel().name, tab.label];
  return `<header class="workspace-head">
    <nav class="breadcrumb" aria-label="Breadcrumb">
      ${crumbs
        .map((crumb, index) => {
          const separator = index > 0 ? '<span class="crumb-sep" aria-hidden="true"> / </span>' : "";
          const isLast = index === crumbs.length - 1;
          return `${separator}<span class="crumb${isLast ? " crumb-current" : ""}"${isLast ? ' aria-current="page"' : ""}>${esc(crumb)}</span>`;
        })
        .join("")}
    </nav>
    <nav class="tabs" aria-label="Workspace views">
      ${VIEW_TABS.map(
        (entry) => `<button type="button" class="tab" data-view="${entry.id}"${entry.id === state.activeView ? ' aria-current="page"' : ""}>
        ${icon(entry.icon)}<span class="tab-label">${esc(entry.label)}</span>
      </button>`,
      ).join("")}
    </nav>
  </header>`;
}

/* ---------- views ---------- */

/* Loose markdown for model answers: escape first, then re-introduce only our own tags. */
function formatAnswer(text) {
  return esc(text)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>");
}

function renderMessage(message) {
  const isUser = message.role === "user";
  const classes = ["message", isUser ? "message-user" : "message-assistant"];
  if (message.pending) classes.push("message-typing");
  if (message.error) classes.push("message-error");
  return `<div class="${classes.join(" ")}">
    <p class="message-role">${isUser ? "You" : "Copilot"}</p>
    <p class="message-bubble">${isUser ? esc(message.text) : formatAnswer(message.text)}</p>
  </div>`;
}

function renderCopilotView() {
  const team = currentTeam();
  const channel = currentChannel();
  return `<section class="view view-copilot" aria-label="Copilot chat">
    <div class="view-head">
      <h2 class="view-title">Copilot</h2>
      <span class="context-pill">${esc(team.name)} \u00b7 ${esc(channel.name)}</span>
    </div>
    <div class="messages" data-message-list aria-live="polite" aria-label="Conversation with Copilot" tabindex="0">
      ${state.messages.map(renderMessage).join("")}
    </div>
    <div class="chips" role="group" aria-label="Suggested prompts">
      ${COPILOT_PROMPTS.map((prompt) => `<button type="button" class="chip" data-prompt="${esc(prompt)}">${esc(prompt)}</button>`).join("")}
    </div>
    <form class="composer" data-copilot-form>
      <label class="sr-only" for="copilot-input">Ask Copilot a question</label>
      <input id="copilot-input" class="composer-input" name="prompt" type="text" autocomplete="off" placeholder="Ask Copilot about payroll deadlines or shared files" />
      <button type="submit" class="composer-send">${icon("send")}<span>Send</span></button>
    </form>
  </section>`;
}

function renderFilesView() {
  const path = `Teams / ${currentTeam().name} / ${currentChannel().name} / Files`;
  return `<section class="view view-files" aria-label="Files">
    <div class="files-toolbar">
      <h2 class="view-title">${icon("folder")}<span>Files</span></h2>
      <p class="files-path">${esc(path)}</p>
      <p class="files-count">${MOCK_FILES.length} items</p>
    </div>
    <div class="file-scroll">
      <table class="file-table">
        <caption class="sr-only">Files in ${esc(path)}</caption>
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Owner</th>
            <th scope="col">Modified</th>
            <th scope="col" class="file-size">Size</th>
          </tr>
        </thead>
        <tbody>
          ${MOCK_FILES.map(
            (file) => `<tr class="file-row">
            <td>
              <span class="file-name">
                <span class="file-badge file-badge-${esc(file.type.toLowerCase())}" aria-hidden="true">${esc(file.type)}</span>
                <span class="file-label">${esc(file.name)}</span>
              </span>
            </td>
            <td class="file-owner">${esc(file.owner)}</td>
            <td class="file-modified">${esc(file.modified)}</td>
            <td class="file-size">${esc(file.size)}</td>
          </tr>`,
          ).join("")}
        </tbody>
      </table>
    </div>
  </section>`;
}

function renderMailView() {
  const [selectedMail] = MOCK_MAIL;
  return `<section class="view view-mail" aria-label="Outlook mail">
    <header class="mail-head">
      <span class="outlook-mark" aria-hidden="true">${icon("mail")}</span>
      <div>
        <h2 class="view-title">Outlook</h2>
        <p class="mail-subtitle">Outlook inside Teams</p>
      </div>
    </header>
    <div class="mail-panes">
      <nav class="folder-rail" aria-label="Mail folders">
        <ul>
          ${MAIL_FOLDERS.map((folder) => {
            const selected = folder.name === "Inbox";
            return `<li class="folder${selected ? " is-selected" : ""}"${selected ? ' aria-current="true"' : ""}>
            ${icon(folder.icon)}<span>${esc(folder.name)}</span>
          </li>`;
          }).join("")}
        </ul>
      </nav>
      <ul class="mail-list" aria-label="Inbox messages">
        ${MOCK_MAIL.map((mail, index) => {
          const selected = index === 0;
          return `<li class="mail-item${selected ? " is-selected" : ""}"${selected ? ' aria-current="true"' : ""}>
          <div class="mail-item-top">
            <span class="mail-sender">${esc(mail.sender)}</span>
            <span class="mail-time">${esc(mail.time)}</span>
          </div>
          <p class="mail-subject">${esc(mail.subject)}</p>
          <p class="mail-preview">${esc(mail.preview)}</p>
        </li>`;
        }).join("")}
      </ul>
      <article class="reading-pane" aria-label="Message">
        <h3 class="reading-subject">${esc(selectedMail.subject)}</h3>
        <p class="reading-meta">
          <span class="reading-sender">${esc(selectedMail.sender)}</span>
          <span class="reading-time">${esc(selectedMail.time)}</span>
        </p>
        <p class="reading-body">${esc(selectedMail.body ?? MAIL_READING_BODY)}</p>
        <p class="reading-note">Read-only demo message</p>
      </article>
    </div>
  </section>`;
}

function renderActiveView() {
  if (state.activeView === "files") return renderFilesView();
  if (state.activeView === "mail") return renderMailView();
  return renderCopilotView();
}

function renderApp() {
  const root = document.getElementById("app");
  if (!root) return;
  root.innerHTML = `<div class="shell">
    ${renderAppRail()}
    ${renderTeamSidebar()}
    <main class="workspace">
      ${renderWorkspaceHeader()}
      ${renderActiveView()}
    </main>
  </div>`;
  bindEvents(root);
}

/* ---------- view switching and URL routing ---------- */

/* The hash carries the location (`#/<teamId>/<channelId>/<view>`) so a refresh or a
   back/forward jump lands on the same view. Nothing is persisted beyond the URL. */

function routeFromState() {
  return `#/${state.selectedTeamId}/${state.selectedChannelId}/${state.activeView}`;
}

function decodePart(part) {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}

function parseRoute(hash) {
  const [teamId, channelId, view] = String(hash ?? "")
    .replace(/^#\/?/, "")
    .split("/")
    .filter(Boolean)
    .map(decodePart);
  return { teamId, channelId, view };
}

function resolveRoute(route) {
  const team =
    MOCK_TEAMS.find((entry) => entry.id === route.teamId) ??
    MOCK_TEAMS.find((entry) => entry.id === state.selectedTeamId) ??
    MOCK_TEAMS[0];
  const channel =
    team.channels.find((entry) => entry.id === route.channelId) ??
    team.channels.find((entry) => entry.id === state.selectedChannelId) ??
    team.channels[0];
  const view = VIEW_TABS.some((entry) => entry.id === route.view) ? route.view : state.activeView;
  return { team, channel, view };
}

function writeRoute({ replace = false } = {}) {
  const target = routeFromState();
  if (window.location.hash === target) return;
  try {
    if (replace) history.replaceState(null, "", target);
    else window.location.hash = target;
  } catch {
    window.location.hash = target;
  }
}

function applyRoute(route, { focusTab = false, force = false } = {}) {
  const changed =
    route.team.id !== state.selectedTeamId ||
    route.channel.id !== state.selectedChannelId ||
    route.view !== state.activeView;
  if (!changed && !force) {
    writeRoute({ replace: true });
    return;
  }
  state.selectedTeamId = route.team.id;
  state.selectedChannelId = route.channel.id;
  state.activeView = route.view;
  renderApp();
  writeRoute({ replace: true });
  if (focusTab && changed) {
    const activeTab = document.querySelector('.tab[aria-current="page"]');
    if (activeTab) activeTab.focus();
  }
}

function setActiveView(view) {
  if (!VIEW_TABS.some((entry) => entry.id === view)) return;
  state.activeView = view;
  renderApp();
  writeRoute();
  const activeTab = document.querySelector('.tab[aria-current="page"]');
  if (activeTab) activeTab.focus();
}

function handleHashChange() {
  applyRoute(resolveRoute(parseRoute(window.location.hash)), { focusTab: true });
}

function init() {
  applyRoute(resolveRoute(parseRoute(window.location.hash)), { force: true });
  window.addEventListener("hashchange", handleHashChange);
}

/* ---------- Copilot behavior ---------- */

/* The question goes to the demo backend (teams/server.py), which sends the whole
   mock-data pack as context and returns the model's answer. Keeping the conversation
   here is enough for follow-up questions; nothing is persisted. */

async function requestCopilotReply(history) {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: history }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error ?? `request failed (${response.status})`);
  if (!data.reply) throw new Error("the model returned an empty answer");
  return data.reply;
}

function conversation() {
  return state.messages
    .filter((message) => !message.pending && !message.error && message.id !== "welcome")
    .map(({ role, text }) => ({ role, content: text }));
}

async function sendCopilotMessage(text) {
  const value = String(text ?? "").trim();
  if (!value || state.pending) return false;
  messageSeq += 1;
  const replyId = `msg-${messageSeq}-reply`;
  state.messages.push({ id: `msg-${messageSeq}`, role: "user", text: value });
  state.messages.push({ id: replyId, role: "assistant", text: "Thinking\u2026", pending: true });
  state.pending = true;
  renderApp();
  scrollMessagesToEnd();

  const history = conversation();
  let answer;
  try {
    answer = await requestCopilotReply(history);
  } catch (error) {
    answer = `Copilot is unavailable: ${error.message}`;
  }

  state.pending = false;
  const reply = state.messages.find((message) => message.id === replyId);
  if (reply) {
    reply.text = answer;
    reply.pending = false;
    reply.error = answer.startsWith("Copilot is unavailable");
  }
  renderApp();
  focusCopilotInput();
  scrollMessagesToEnd();
  return true;
}

function focusCopilotInput() {
  const input = document.getElementById("copilot-input");
  if (input) input.focus();
}

function scrollMessagesToEnd() {
  const list = document.querySelector("[data-message-list]");
  if (list) list.scrollTop = list.scrollHeight;
}

/* ---------- events ---------- */

function handleClick(event) {
  const chip = event.target.closest("[data-prompt]");
  if (chip) {
    sendCopilotMessage(chip.dataset.prompt);
    return;
  }
  const tab = event.target.closest("[data-view]");
  if (tab) setActiveView(tab.dataset.view);
}

function handleSubmit(event) {
  const form = event.target.closest("form[data-copilot-form]");
  if (!form) return;
  event.preventDefault();
  const input = form.querySelector("#copilot-input");
  if (!input) return;
  if (!sendCopilotMessage(input.value)) focusCopilotInput();
}

function bindEvents(root) {
  if (root.dataset.bound === "true") return;
  root.dataset.bound = "true";
  root.addEventListener("click", handleClick);
  root.addEventListener("submit", handleSubmit);
}

init();

/* Real mock data: /api/files and /api/mail come from server.py reading mock-data/.
   Without that server the bundled demo rows above stay in place. */
async function loadDemoData() {
  try {
    const [files, mail] = await Promise.all([
      fetch("/api/files").then((response) => response.json()),
      fetch("/api/mail").then((response) => response.json()),
    ]);
    if (Array.isArray(files.files) && files.files.length) MOCK_FILES = files.files;
    if (Array.isArray(mail.messages) && mail.messages.length) MOCK_MAIL = mail.messages;
    renderApp();
  } catch (error) {
    /* keep the bundled rows */
  }
}

loadDemoData();
