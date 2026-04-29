const APP_CONFIG = window.APP_CONFIG || {};
const MODEL_NAME = APP_CONFIG.modelName || 'Gemini 2.5 Flash';

let sessionId = localStorage.getItem('sessionId') || null;

const form = document.getElementById('chatForm');
const input = document.getElementById('userInput');
const chatHistory = document.getElementById('chatHistory');
const sendBtn = document.getElementById('sendBtn');
const typingIndicator = document.getElementById('typingIndicator');

const headerModelName = document.getElementById('headerModelName');
const footerModelName = document.getElementById('footerModelName');

function hydrateModelLabels() {
    [headerModelName, footerModelName].forEach((node) => {
        if (node) node.textContent = MODEL_NAME;
    });
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function renderWelcomeMessage() {
    chatHistory.innerHTML = `
        <div class="message system-msg">
            <div class="avatar">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                    <circle cx="12" cy="12" r="10"></circle>
                    <path d="M12 16v-4"></path>
                    <path d="M12 8h.01"></path>
                </svg>
            </div>
            <div class="msg-content">
                <strong>Hello! I’m your Workspace Workflow Orchestrator.</strong>
                <p>I can help you manage meetings, emails, and documents across Google Calendar, Gmail, and Docs. \n
                Try asking me to schedule a meeting, brief your day, or summarize your inbox.</p>
            </div>
        </div>
    `;
}

window.setInput = (text) => {
    input.value = text;
    input.focus();
    autoResizeTextarea();
};

window.startNewChat = () => {
    localStorage.removeItem('sessionId');
    sessionId = null;
    renderWelcomeMessage();
    input.focus();
    autoResizeTextarea();
};

marked.setOptions({
    breaks: true,
    gfm: true,
});

const autoResizeTextarea = () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
};

input.addEventListener('input', autoResizeTextarea);

input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        form.dispatchEvent(new Event('submit'));
    }
});

function formatWorkflowSteps(workflows = []) {
    if (!Array.isArray(workflows) || workflows.length === 0) {
        return '';
    }

    const steps = workflows.map((w) => {
        const agent = escapeHtml(w.agent || 'agent');
        const action = escapeHtml(w.action || 'executed step');

        return `
            <div class="workflow-step">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <polyline points="20 6 9 17 4 12"></polyline>
                </svg>
                <span>✔ <strong>${agent}</strong> → ${action}</span>
            </div>
        `;
    }).join('');

    return `
        <div class="workflow-box">
            <div class="workflow-title">⚙️ Workflow executed</div>
            ${steps}
        </div>
    `;
}

function appendMessage(role, text, workflows = []) {
    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}-msg`;

    const avatar = role === 'user'
        ? `
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
                <circle cx="12" cy="7" r="4"></circle>
            </svg>
        `
        : `
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <circle cx="12" cy="12" r="10"></circle>
                <path d="M12 16v-4"></path>
                <path d="M12 8h.01"></path>
            </svg>
        `;

    const workflowHTML = role === 'system' ? formatWorkflowSteps(workflows) : '';
    const contentParsed = role === 'system'
        ? marked.parse(text || '')
        : `<p>${escapeHtml(text || '')}</p>`;

    msgDiv.innerHTML = `
        <div class="avatar">${avatar}</div>
        <div class="msg-content">
            ${contentParsed}
            ${workflowHTML}
        </div>
    `;

    chatHistory.appendChild(msgDiv);
    chatHistory.scrollTop = chatHistory.scrollHeight;
}

form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const message = input.value.trim();
    if (!message) return;

    input.value = '';
    autoResizeTextarea();
    sendBtn.disabled = true;

    appendMessage('user', message);
    typingIndicator.classList.remove('hidden');
    chatHistory.scrollTop = chatHistory.scrollHeight;

    try {
        const payload = { message };
        if (sessionId) {
            payload.session_id = sessionId;
        }

        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        if (!response.ok) {
            const errData = await response.json().catch(() => ({}));
            throw new Error(errData.detail || `Server returned status ${response.status}`);
        }

        const data = await response.json();

        sessionId = data.session_id;
        localStorage.setItem('sessionId', sessionId);

        appendMessage('system', data.response, data.workflow_steps || []);
    } catch (error) {
        console.error('Error:', error);
        appendMessage(
            'system',
            `**Something went wrong.** Please try again or check your connection.\n\n${escapeHtml(error.message || 'Unknown error.')}`
        );
    } finally {
        typingIndicator.classList.add('hidden');
        sendBtn.disabled = false;
        input.focus();
    }
});

hydrateModelLabels();
renderWelcomeMessage();
autoResizeTextarea();