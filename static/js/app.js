/*
 * Nuage Libre: chat front-end (vanilla JavaScript, no framework).
 *
 * Responsibilities: talk to the FastAPI back-end (/api/...), display the
 * streamed answers, manage the list of saved conversations, the model menu
 * and the RAG switch. Visual concerns live elsewhere: css/ for the look,
 * js/theme.js for the theme switcher, js/layout.js for the split-screen,
 * js/sky.js for the animated backgrounds. The file manager (left panel) is
 * js/files.js.
 *
 * Security rules (XSS = Cross-Site Scripting prevention):
 *
 * 1. By default, text coming from the server, the user or the model is
 *    inserted with `textContent`: the browser displays it as plain text and
 *    never executes it, even if it contains HTML tags.
 * 2. ONE exception: the model's answers are Markdown and must be shown as
 *    formatted HTML. They go through renderMarkdown(), which converts them
 *    with marked and then passes the HTML through DOMPurify before it
 *    touches `innerHTML`. DOMPurify keeps harmless formatting (bold, lists,
 *    tables, code...) and strips everything that can run code: script tags,
 *    event handlers (onerror=...), javascript: links...
 *    `innerHTML` must never be used anywhere else.
 * 3. Icons and the mascot are cloned from <template> elements of
 *    index.html (cloneNode), so no HTML string is ever parsed here.
 */
(() => {
  'use strict';

  // ---------------------------------------------------------------- state
  // Id of the conversation shown on screen; null = a new, unsaved chat.
  let currentConversationId = null;
  let isGenerating = false;

  // ------------------------------------------------------------- elements
  const $ = (id) => document.getElementById(id);
  const chatForm = $('chat-form');
  const userInput = $('user-input');
  const sendBtn = $('send-btn');
  const chatHistory = $('chat-history');
  const messagesEl = $('messages');
  const listEl = $('conversation-list');
  const sidebar = $('sidebar');
  const overlay = $('sidebar-overlay');
  const sidebarToggle = $('sidebar-toggle');
  const jumpBtn = $('jump-latest');

  /** Return a fresh copy of a <template>'s content (icon, mascot...). */
  const cloneTemplate = (id) => $(id).content.firstElementChild.cloneNode(true);

  // Every mascot slot of the page: logo, empty cloud, drag & drop overlay.
  document.querySelectorAll('.js-mascot').forEach((slot) => slot.appendChild(cloneTemplate('mascot-template')));

  // ------------------------------------------------------ follow scrolling
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  /**
   * "Follow the output" scrolling for a scrollable element, like a terminal
   * or a live chat:
   *
   * - while the reader is at the bottom, new content keeps the view at the
   *   bottom (the text being written stays in sight);
   * - as soon as they scroll UP, following stops: the view stays exactly
   *   where they put it, so they can read calmly while the model writes;
   * - scrolling back down to the bottom (or calling follow()) resumes it.
   *
   * Why the wheel, touch and keyboard are watched, and not only "scroll":
   * a model writes 30 to 50 tokens per second, so the content grows (and
   * we scroll down) between two screen frames. The "scroll" event is sent
   * asynchronously, AFTER our own scroll may already have put the view back
   * at the bottom: the user's gesture would be undone before we could even
   * see it, which was the bug. wheel, touchmove and keydown are sent BEFORE
   * the browser scrolls, so following stops at the very first notch.
   *
   * Returns { changed(), follow(smooth), isFollowing(), distance() }.
   * `onChange` is called after every change of position or content.
   */
  function followScroll(el, { onChange = () => {} } = {}) {
    // "At the bottom" tolerance in px: absorbs sub-pixel rounding and
    // zoom, without catching a reader who moved up even a few lines.
    const NEAR_BOTTOM = 8;
    let following = true;
    let lastTop = el.scrollTop;
    let frame = 0;
    let touchY = null;

    let pressTop = null;
    let pressFollowing = false;

    const distance = () => el.scrollHeight - el.scrollTop - el.clientHeight;

    function stop() {
      following = false;
      onChange();
    }

    /** True if the gesture scrolls a box INSIDE `el` (e.g. the reasoning
     *  panel within the chat) that can still go up: then `el` itself does
     *  not move, and must keep following. */
    function innerBoxScrolls(target) {
      for (let node = target; node && node !== el; node = node.parentElement) {
        if (node.scrollTop > 0 && node.scrollHeight > node.clientHeight) {
          const { overflowY } = getComputedStyle(node);
          if (overflowY === 'auto' || overflowY === 'scroll') return true;
        }
      }
      return false;
    }

    // Intentions to go UP, seen before the browser moves anything.
    el.addEventListener('wheel', (event) => {
      if (event.deltaY < 0 && !innerBoxScrolls(event.target)) stop();
    }, { passive: true });
    el.addEventListener('touchstart', (event) => { touchY = event.touches[0].clientY; }, { passive: true });
    el.addEventListener('touchmove', (event) => {
      // Finger moving DOWN the screen = content scrolling UP.
      if (touchY !== null && event.touches[0].clientY > touchY + 4 && !innerBoxScrolls(event.target)) stop();
    }, { passive: true });
    el.addEventListener('keydown', (event) => {
      const up = ['ArrowUp', 'PageUp', 'Home'].includes(event.key) || (event.key === ' ' && event.shiftKey);
      if (up && !innerBoxScrolls(event.target)) stop();
    });
    // A press on the element itself (not on a message) may be the start of
    // a scrollbar drag: pause while the button is down. On release, if the
    // view did not move (a simple click in an empty area), carry on as before.
    el.addEventListener('pointerdown', (event) => {
      if (event.target !== el) return;
      pressTop = el.scrollTop;
      pressFollowing = following;
      stop();
      window.addEventListener('pointerup', () => {
        const unmoved = el.scrollTop === pressTop;
        pressTop = null;
        if ((pressFollowing && unmoved) || distance() <= NEAR_BOTTOM) {
          following = true;
          changed();
        }
      }, { once: true });
    });

    // Every other way of moving (scrollbar, find in page, screen reader...)
    // is seen here, once the browser has scrolled.
    el.addEventListener('scroll', () => {
      const top = el.scrollTop;
      if (distance() <= NEAR_BOTTOM) {
        // Back at the bottom by going down: follow again. (When content
        // above shrinks, the browser moves the view UP while it stays at
        // the bottom: that must not change anything.)
        if (top >= lastTop) following = true;
      } else if (top < lastTop) {
        following = false;
      }
      lastTop = top;
      onChange();
    }, { passive: true });

    /** To call after every change of the content (new token, new message). */
    function changed() {
      if (following && !frame) {
        // At most one scroll per screen frame, after the frame's rendering.
        frame = requestAnimationFrame(() => {
          frame = 0;
          if (!following) return; // the user scrolled up meanwhile
          el.scrollTop = el.scrollHeight;
          lastTop = el.scrollTop;
          onChange();
        });
      }
      onChange();
    }

    /** Go (back) to the bottom and follow from now on. */
    function follow({ smooth = false } = {}) {
      following = true;
      if (smooth && !reducedMotion.matches) {
        el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
      } else {
        el.scrollTop = el.scrollHeight;
        lastTop = el.scrollTop;
      }
      onChange();
    }

    return { changed, follow, isFollowing: () => following, distance };
  }

  /*
   * "Latest" button, above the composer: shown when the latest message is
   * out of view. While the model is writing below, it glows softly
   * (.is-live) to say that new text is arriving.
   */
  const JUMP_THRESHOLD = 160; // px below the view before the button appears
  function updateJumpButton() {
    const away = chatFollow.distance() > JUMP_THRESHOLD;
    if (jumpBtn.hidden === away) jumpBtn.hidden = !away;
    jumpBtn.classList.toggle('is-live', away && isGenerating);
  }
  const chatFollow = followScroll(chatHistory, { onChange: updateJumpButton });

  jumpBtn.addEventListener('click', () => {
    const hadFocus = document.activeElement === jumpBtn;
    chatFollow.follow({ smooth: true });
    // The button disappears: a keyboard user must not lose their place.
    if (hadFocus) (userInput.disabled ? chatHistory : userInput).focus({ preventScroll: true });
  });

  // ------------------------------------------------------------ rendering

  // Clickable example prompts on the welcome screen.
  const SUGGESTIONS = [
    'Explain Docker volumes simply',
    'Write a Python function that renames photos by date',
    'Give me 3 ideas to secure a home server',
  ];

  /** Welcome screen of a new chat: big floating mascot + suggestions. */
  function showWelcome() {
    messagesEl.replaceChildren();

    const welcome = document.createElement('section');
    welcome.className = 'welcome';
    welcome.setAttribute('aria-label', 'Welcome');

    const mascot = document.createElement('div');
    mascot.className = 'welcome-mascot';
    mascot.appendChild(cloneTemplate('mascot-template'));

    // h3: the page title (h1) is the brand, the chat panel is an h2.
    const title = document.createElement('h3');
    title.className = 'welcome-title';
    title.textContent = 'Hello! I am your little cloud.';

    const subtitle = document.createElement('p');
    subtitle.className = 'welcome-subtitle';
    subtitle.textContent = 'I run entirely on your own machine: nothing you write here ever leaves it. What shall we do today?';

    const suggestions = document.createElement('div');
    suggestions.className = 'suggestions';
    for (const text of SUGGESTIONS) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'suggestion';
      chip.textContent = text;
      chip.addEventListener('click', () => {
        if (isGenerating) return;
        sendMessage(text);
      });
      suggestions.appendChild(chip);
    }

    welcome.append(mascot, title, subtitle, suggestions);
    messagesEl.appendChild(welcome);
  }

  /** Remove the welcome screen (when the first message is sent). */
  function hideWelcome() {
    messagesEl.querySelector('.welcome')?.remove();
  }

  /**
   * Create an empty message bubble and return handles to its parts.
   * role: 'user' | 'assistant' | 'error'
   */
  function appendBubble(role) {
    hideWelcome();
    const wrapper = document.createElement('div');
    wrapper.className = `msg msg--${role === 'user' ? 'user' : 'ai'}`;
    const bubble = document.createElement('div');
    const parts = { wrapper, bubble };

    if (role === 'user') {
      bubble.className = 'bubble bubble--user';
      parts.answer = bubble;
    } else if (role === 'error') {
      bubble.className = 'bubble bubble--error';
      bubble.setAttribute('role', 'alert');
      parts.answer = bubble;
    } else {
      bubble.className = 'bubble bubble--ai';

      // Collapsible block (<details>) for the model's reasoning ("thinking").
      const details = document.createElement('details');
      details.className = 'thought';
      details.hidden = true;
      const summary = document.createElement('summary');
      summary.textContent = 'Thought process';
      const thought = document.createElement('div');
      thought.className = 'thought-text';
      // The reasoning box scrolls on its own (max-height): it follows the
      // model's thoughts the same way, and can be read while they stream.
      thought.tabIndex = 0; // a scrollable region must be reachable by keyboard
      parts.thoughtFollow = followScroll(thought);
      details.append(summary, thought);

      // Answer area, styled by css/markdown.css.
      const answer = document.createElement('div');
      answer.className = 'markdown';
      // Generation statistics (tokens, speed), shown under the answer.
      const stats = document.createElement('div');
      stats.className = 'stats';
      stats.hidden = true;

      bubble.append(details, answer, stats);
      Object.assign(parts, { details, summary, thought, answer, stats });
    }

    wrapper.appendChild(bubble);
    messagesEl.appendChild(wrapper);
    chatFollow.changed();
    return parts;
  }

  /** Three bouncing mini clouds, shown until the first token arrives. */
  function showTyping(el) {
    const typing = document.createElement('span');
    typing.className = 'typing';
    typing.setAttribute('role', 'status');
    typing.setAttribute('aria-label', 'The model is thinking');
    typing.append(document.createElement('span'), document.createElement('span'), document.createElement('span'));
    el.replaceChildren(typing);
  }

  /** "hf.co/user/Model-GGUF:Q4_K_M" -> "Model-GGUF:Q4_K_M" (short display name). */
  const shortModelName = (name) => (name || '').split('/').pop();

  /** Footer of an answer: model name + generation statistics. */
  function showStats(parts, model, tokenCount, generationMs) {
    const items = [];
    if (model) items.push(shortModelName(model));
    if (tokenCount && generationMs) {
      const tps = (tokenCount / (generationMs / 1000)).toFixed(1);
      items.push(`${tokenCount} tokens`, `${(generationMs / 1000).toFixed(1)} s`, `${tps} tok/s`);
    }
    if (!items.length) return;
    parts.stats.textContent = items.join(' · ');
    parts.stats.title = model || '';
    parts.stats.hidden = false;
  }

  function showThought(parts, text) {
    if (!text) return;
    parts.details.hidden = false;
    parts.thought.textContent = text;
  }

  // ------------------------------------------------------------- markdown
  // GFM = GitHub Flavored Markdown (tables, ~~strike~~, task lists...).
  marked.setOptions({ gfm: true, breaks: false });

  // ---------------------------------------------------------------- math
  /*
   * LaTeX formulas, rendered with KaTeX (vendored in static/vendor/).
   *
   * Models write maths in four notations, all supported:
   *     $x^2$        \( x^2 \)       -> inline formula, inside a sentence
   *     $$x^2$$      \[ x^2 \]       -> display formula, centred on its own line
   *
   * HOW IT PLUGS INTO THE PIPELINE
   * We add a small "extension" to marked: custom tokenizers that recognise
   * the formulas BEFORE marked applies its usual Markdown rules. This order
   * matters: in "$a_1 * b_1$", Markdown would otherwise turn "_1 * b_" into
   * italics, and "\(" would lose its backslash (it is a Markdown escape).
   * The formula is then rendered by KaTeX to HTML, and that HTML goes, with
   * the rest of the answer, through DOMPurify:
   *
   *     Markdown --marked + math extension--> HTML --DOMPurify--> innerHTML
   *
   * SECURITY
   * - KaTeX runs with trust: false, so commands that could create links or
   *   raw HTML (\href, \url, \htmlClass, \includegraphics...) are refused.
   * - KaTeX's output is NOT trusted blindly: it is sanitised by DOMPurify
   *   like everything else (defence in depth). DOMPurify keeps what KaTeX
   *   needs (span + class/style, MathML, SVG paths) and nothing executable.
   * - maxSize / maxExpand limit huge sizes and recursive macros, so a
   *   malicious or buggy formula cannot freeze the browser.
   * - An invalid formula is shown in red with its source (throwOnError:
   *   false) instead of breaking the whole answer.
   *
   * Avoiding false positives with dollar signs ("it costs $5 and $10"):
   * like Pandoc, an inline $...$ formula must not start with a space, must
   * not end with a space, and its closing $ must not be followed by a digit.
   * One tolerated exception: "$ x^2 $" with a space on BOTH sides.
   */
  const MATH_OPTIONS = {
    throwOnError: false,
    trust: false,
    strict: 'ignore',         // accept common non-standard LaTeX without warnings
    output: 'htmlAndMathml',  // HTML for the eyes, MathML for screen readers
    maxSize: 20,              // em: caps \rule{1000em}{1000em}-style abuse
    maxExpand: 500,           // caps recursive macro expansion
    // Colour of invalid formulas. KaTeX writes it into a style attribute, so
    // a CSS variable works: the colour follows the theme (KaTeX's default
    // dark red is unreadable on the night sky).
    errorColor: 'var(--error-text)',
  };

  // Streaming re-renders the answer up to 60 times per second: without a
  // cache, every formula would be re-typeset on every frame. Formulas never
  // change once complete, so their HTML is remembered (bounded size).
  const mathCache = new Map();
  const MATH_CACHE_LIMIT = 500;

  function renderTex(tex, displayMode) {
    const key = `${displayMode ? 'D' : 'I'}${tex}`;
    let html = mathCache.get(key);
    if (html === undefined) {
      html = katex.renderToString(tex, { ...MATH_OPTIONS, displayMode });
      if (mathCache.size >= MATH_CACHE_LIMIT) mathCache.delete(mathCache.keys().next().value);
      mathCache.set(key, html);
    }
    return html;
  }

  // Block formulas: a paragraph made only of $$...$$ or \[...\] (may span lines).
  const BLOCK_MATH = /^ {0,3}(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\])[ \t]*(?:\n+|$)/;
  // Inline formulas, tried in this order at the current position.
  const INLINE_MATH = [
    { re: /^\$\$([\s\S]+?)\$\$/, display: true },
    { re: /^\\\[([\s\S]+?)\\\]/, display: true },
    { re: /^\\\(([\s\S]+?)\\\)/, display: false },
    // Strict (Pandoc) form: $x$ with no space just inside the dollars.
    { re: /^\$(?![\s$])((?:\\.|[^\\$\n])+?)(?<!\s)\$(?!\d)/, display: false },
    // Spaced form "$ x^2 $", which real models write often (found during the
    // end-to-end test with qwen3:4b). Spaces on BOTH sides are required, so
    // prices such as "$5 and $10" are still never taken for a formula.
    { re: /^\$[ \t]+((?:\\.|[^\\$\n])+?)[ \t]+\$(?!\d)/, display: false },
  ];

  marked.use({
    extensions: [
      {
        name: 'mathBlock',
        level: 'block',
        // Where a block formula could start, so marked stops a paragraph there.
        start(src) {
          const match = src.match(/^ {0,3}(?:\$\$|\\\[)/m);
          return match ? match.index : undefined;
        },
        tokenizer(src) {
          const match = BLOCK_MATH.exec(src);
          if (!match) return undefined;
          return { type: 'mathBlock', raw: match[0], text: (match[1] ?? match[2]).trim() };
        },
        renderer(token) {
          return `<div class="math-block">${renderTex(token.text, true)}</div>\n`;
        },
      },
      {
        name: 'mathInline',
        level: 'inline',
        // "\$" is included so that an escaped dollar is left to marked's
        // escape rule (it then shows a literal "$", not a formula).
        start(src) {
          const index = src.search(/\\[$([]|\$/);
          return index < 0 ? undefined : index;
        },
        tokenizer(src) {
          for (const { re, display } of INLINE_MATH) {
            const match = re.exec(src);
            if (match) return { type: 'mathInline', raw: match[0], text: match[1].trim(), display };
          }
          return undefined;
        },
        renderer(token) {
          return renderTex(token.text, token.display);
        },
      },
    ],
  });

  // KaTeX's MathML contains <annotation> holding the TeX source. DOMPurify
  // does not allow <semantics>/<annotation> (to avoid MathML namespace
  // tricks) and would remove the tags but KEEP their text, so screen readers
  // would read the raw TeX after the formula. We empty <annotation> first,
  // then DOMPurify removes the empty element.
  DOMPurify.addHook('uponSanitizeElement', (node, data) => {
    if (data.tagName === 'annotation') node.textContent = '';
  });

  // DOMPurify hook, run on every element it keeps: links open in a new tab,
  // and rel="noopener noreferrer" prevents the opened page from controlling
  // this one through window.opener (reverse tabnabbing).
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });

  /**
   * Convert a Markdown string to safe HTML and display it in `el`.
   * marked produces HTML (which may contain anything the model wrote,
   * including raw HTML); DOMPurify then removes every dangerous element and
   * attribute. Only the sanitised result reaches innerHTML.
   */
  function renderMarkdown(el, markdownText) {
    const unsafeHtml = marked.parse(markdownText);
    el.innerHTML = DOMPurify.sanitize(unsafeHtml);
  }

  /**
   * Copy text to the clipboard. navigator.clipboard only exists on "secure"
   * pages (https:// or localhost). Over Tailscale the UI is served from
   * http://100.x.x.x, which is not considered secure, so we fall back to the
   * older execCommand('copy') technique with a temporary hidden textarea.
   */
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch {
        // Permission refused by the browser: try the fallback below.
      }
    }
    const tmp = document.createElement('textarea');
    tmp.value = text;
    tmp.style.position = 'fixed';
    tmp.style.opacity = '0';
    document.body.appendChild(tmp);
    tmp.select();
    const ok = document.execCommand('copy');
    tmp.remove();
    if (!ok) throw new Error('Copy refused by the browser');
  }

  /**
   * Final touches on code blocks once an answer is complete: syntax
   * highlighting, plus a header showing the language and a "Copy" button.
   * Done only at the end (not on every streamed token) because highlighting
   * is relatively expensive.
   */
  function enhanceCodeBlocks(el) {
    el.querySelectorAll('pre > code').forEach((code) => {
      const pre = code.parentElement;
      if (pre.dataset.enhanced) return;
      pre.dataset.enhanced = 'true';

      hljs.highlightElement(code);
      // marked marks fenced blocks as class="language-python" etc.
      const langClass = [...code.classList].find((c) => c.startsWith('language-'));
      const language = langClass ? langClass.slice('language-'.length) : 'code';

      const header = document.createElement('div');
      header.className = 'code-header';
      const label = document.createElement('span');
      label.textContent = language;
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'copy-btn';
      copy.textContent = 'Copy';
      copy.setAttribute('aria-label', `Copy ${language} code`);
      copy.addEventListener('click', async () => {
        try {
          await copyText(code.textContent);
          copy.textContent = 'Copied!';
        } catch {
          copy.textContent = 'Failed';
        }
        setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
      });
      header.append(label, copy);
      pre.prepend(header);
    });
  }

  // ---------------------------------------------------------- model menu
  const modelSelect = $('model-select');
  const MODEL_STORAGE_KEY = 'nuage-libre:model';

  // localStorage can throw (private browsing, blocked storage): the menu
  // must keep working, the choice is just not remembered in that case.
  function savedModel() {
    try { return localStorage.getItem(MODEL_STORAGE_KEY); } catch { return null; }
  }
  function saveModel(name) {
    try { localStorage.setItem(MODEL_STORAGE_KEY, name); } catch { /* ignore */ }
  }

  /** Human-readable size: 2500000000 -> "2.5 GB". */
  const formatSize = (bytes) => `${(bytes / 1e9).toFixed(1)} GB`;

  async function loadModels() {
    let models = [];
    try {
      const response = await fetch('/api/models');
      if (response.ok) models = await response.json();
    } catch { /* handled below */ }

    modelSelect.replaceChildren();
    if (!models.length) {
      const option = new Option('No model available', '');
      option.disabled = true;
      modelSelect.append(option);
      return;
    }
    for (const m of models) {
      // e.g. "qwen3:4b — 4.0B · 2.5 GB · 🧠"   (🧠 = shows its reasoning)
      const details = [m.parameter_size, formatSize(m.size_bytes)].filter(Boolean);
      if (m.thinking) details.push('🧠');
      if (m.is_default) details.push('default');
      const option = new Option(`${shortModelName(m.name)} — ${details.join(' · ')}`, m.name);
      option.title = m.name;
      modelSelect.append(option);
    }
    // Restore the last choice if that model is still installed, else the default.
    const remembered = savedModel();
    const fallback = models.find((m) => m.is_default) || models[0];
    modelSelect.value = models.some((m) => m.name === remembered) ? remembered : fallback.name;
  }

  /** Select `name` in the menu if it is installed (used when opening a conversation). */
  function selectModel(name) {
    if ([...modelSelect.options].some((o) => o.value === name)) {
      modelSelect.value = name;
      saveModel(name);
    }
  }

  modelSelect.addEventListener('change', () => saveModel(modelSelect.value));

  // -------------------------------------------------------------- sidebar
  function openSidebar(open) {
    sidebar.classList.toggle('is-open', open);
    overlay.hidden = !open;
    sidebarToggle.setAttribute('aria-expanded', String(open));
  }
  sidebarToggle.addEventListener('click', () => openSidebar(true));
  overlay.addEventListener('click', () => openSidebar(false));
  // Escape closes the mobile sidebar (keyboard users must never be stuck).
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sidebar.classList.contains('is-open')) {
      openSidebar(false);
      sidebarToggle.focus();
    }
  });

  async function refreshConversationList() {
    let conversations;
    try {
      const response = await fetch('/api/conversations');
      if (!response.ok) return;
      conversations = await response.json();
    } catch {
      return; // server unreachable: keep the current list, health dot shows it
    }
    listEl.replaceChildren();
    $('empty-list').hidden = conversations.length > 0;

    for (const conv of conversations) {
      const li = document.createElement('li');
      const active = conv.id === currentConversationId;
      li.className = `conversation-item${active ? ' is-active' : ''}`;

      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'conversation-open';
      const title = document.createElement('span');
      title.className = 'conversation-title';
      title.textContent = conv.title;
      open.appendChild(title);
      open.title = conv.title;
      if (active) open.setAttribute('aria-current', 'true');
      open.addEventListener('click', () => loadConversation(conv.id));

      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'conversation-delete';
      del.appendChild(cloneTemplate('icon-trash'));
      del.setAttribute('aria-label', `Delete conversation "${conv.title}"`);
      del.addEventListener('click', () => deleteConversation(conv.id, conv.title));

      li.append(open, del);
      listEl.appendChild(li);
    }
  }

  async function loadConversation(id) {
    if (isGenerating) return;
    const response = await fetch(`/api/conversations/${id}`);
    if (!response.ok) {
      appendBubble('error').answer.textContent = 'Could not load this conversation.';
      return;
    }
    const conv = await response.json();
    currentConversationId = conv.id;
    messagesEl.replaceChildren();
    for (const msg of conv.messages) {
      const parts = appendBubble(msg.role);
      if (msg.role === 'assistant') {
        renderMarkdown(parts.answer, msg.content);
        enhanceCodeBlocks(parts.answer);
        showThought(parts, msg.thinking);
        showStats(parts, msg.model, msg.token_count, msg.generation_ms);
      } else {
        parts.answer.textContent = msg.content; // user text: never interpreted
      }
    }
    // An opened conversation starts at its latest message.
    chatFollow.follow();
    // Continue with the model of the latest answer.
    selectModel(conv.model);
    openSidebar(false);
    refreshConversationList();
    userInput.focus();
  }

  async function deleteConversation(id, title) {
    if (!confirm(`Delete "${title}"? This cannot be undone.`)) return;
    const response = await fetch(`/api/conversations/${id}`, { method: 'DELETE' });
    if (response.ok && id === currentConversationId) newChat();
    refreshConversationList();
  }

  function newChat() {
    if (isGenerating) return;
    currentConversationId = null;
    showWelcome();
    chatFollow.follow();
    openSidebar(false);
    refreshConversationList();
    userInput.focus();
  }
  $('new-chat-btn').addEventListener('click', newChat);
  // Compact copy in the chat header, shown when the history is a drawer.
  $('new-chat-mini').addEventListener('click', newChat);

  // The layout changed (js/layout.js): a drawer left open would cover the
  // chat when it comes back, and the latest message should be in view.
  document.addEventListener('viewchange', (event) => {
    openSidebar(false);
    // The panel's width changed, so its text was re-wrapped: put the latest
    // message back in view, unless the reader was reading further up.
    if (event.detail.view !== 'files' && chatFollow.isFollowing()) chatFollow.follow();
  });

  // ------------------------------------------------------------------ RAG
  /*
   * "Use my documents as context" switch: preparation for v0.7.
   * Today it only records the user's choice and says honestly, in the
   * status line above the composer, that answers do not use documents yet.
   * In v0.7 the chat request will carry it, and the server will search the
   * embeddings of the files marked is_vectorized (pgvector) to add the most
   * relevant passages to the prompt.
   */
  const ragToggle = $('rag-toggle');
  const ragStatus = $('rag-status');
  const RAG_STORAGE_KEY = 'nuage-libre:rag';
  let documentsInMemory = 0;

  const ragEnabled = () => ragToggle.getAttribute('aria-checked') === 'true';

  function updateRagStatus() {
    if (!ragEnabled()) {
      ragStatus.hidden = true;
      return;
    }
    ragStatus.textContent = documentsInMemory
      ? `RAG on · ${documentsInMemory} document${documentsInMemory > 1 ? 's' : ''} in the AI memory, used by answers from v0.7`
      : 'RAG on · no document in the AI memory yet: answers use the model\'s own knowledge only';
    ragStatus.hidden = false;
  }

  function setRag(on, { remember = true } = {}) {
    ragToggle.setAttribute('aria-checked', String(on));
    if (remember) {
      try { localStorage.setItem(RAG_STORAGE_KEY, on ? 'on' : 'off'); } catch { /* not remembered */ }
    }
    updateRagStatus();
  }

  ragToggle.addEventListener('click', () => setRag(!ragEnabled()));

  // Sent by js/files.js with the totals of the whole cloud (all folders).
  document.addEventListener('cloud:changed', (event) => {
    documentsInMemory = event.detail.summary.in_ai_memory;
    updateRagStatus();
  });

  let savedRag = null;
  try { savedRag = localStorage.getItem(RAG_STORAGE_KEY); } catch { /* storage blocked */ }
  setRag(savedRag === 'on', { remember: false });

  // ----------------------------------------------------------------- chat
  /**
   * Read an NDJSON stream (one JSON object per line) and call onEvent for
   * each object. Network chunks do not respect line boundaries, so we
   * buffer the incomplete last line until the next chunk arrives.
   */
  async function readEvents(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) if (line.trim()) onEvent(JSON.parse(line));
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer));
  }

  function setBusy(busy) {
    isGenerating = busy;
    userInput.disabled = busy;
    sendBtn.disabled = busy;
    // Lets the CSS animate the mascot while the model works.
    document.body.classList.toggle('is-generating', busy);
    updateJumpButton();
  }

  async function sendMessage(message) {
    // Sending a message means "show me the answer": follow from the bottom.
    chatFollow.follow();
    appendBubble('user').answer.textContent = message;
    setBusy(true);

    const ai = appendBubble('assistant');
    showTyping(ai.answer);
    ai.bubble.classList.add('is-waiting');
    let thought = '', answer = '', errorText = null, started = false, wasNew = false;

    // Re-rendering the whole Markdown on every token would be wasteful
    // (dozens of tokens per second). Instead we schedule at most one render
    // per screen frame (~60/s max) with requestAnimationFrame.
    let renderScheduled = false;
    let streamFinished = false;
    const scheduleRender = () => {
      if (renderScheduled) return;
      renderScheduled = true;
      requestAnimationFrame(() => {
        renderScheduled = false;
        // A frame scheduled just before the end must not overwrite the
        // final render (it would erase highlighting and copy buttons).
        if (streamFinished) return;
        renderMarkdown(ai.answer, answer);
        chatFollow.changed();
      });
    };

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: currentConversationId,
          message,
          // Empty value (no model available) -> let the server decide.
          model: modelSelect.value || null,
        }),
      });

      if (!response.ok) {
        // FastAPI errors look like {"detail": "..."} (or a list for 422).
        const body = await response.json().catch(() => ({}));
        errorText = typeof body.detail === 'string' ? body.detail : `Server error (${response.status})`;
      } else {
        await readEvents(response, (evt) => {
          if (!started && (evt.type === 'thinking' || evt.type === 'content')) {
            started = true;
            ai.bubble.classList.remove('is-waiting');
            ai.answer.replaceChildren();
          }
          switch (evt.type) {
            case 'conversation':
              currentConversationId = evt.id;
              wasNew = evt.is_new;
              break;
            case 'thinking':
              thought += evt.text;
              showThought(ai, thought);
              ai.thoughtFollow.changed();
              if (!answer) {
                ai.details.open = true;
                ai.details.classList.add('is-thinking');
                ai.summary.textContent = 'Thought process (thinking…)';
              }
              break;
            case 'content':
              if (!answer) {
                ai.details.open = false;
                ai.details.classList.remove('is-thinking');
                ai.summary.textContent = 'Thought process';
              }
              answer += evt.text;
              scheduleRender();
              break;
            case 'error':
              errorText = evt.text;
              break;
            case 'done':
              showStats(ai, evt.model, evt.token_count, evt.generation_ms);
              break;
          }
          chatFollow.changed();
        });
      }
    } catch (err) {
      errorText = `Network error: ${err.message}`;
    }

    ai.bubble.classList.remove('is-waiting');
    streamFinished = true;
    if (answer) {
      // Final, complete render (a scheduled frame may not have run yet),
      // then syntax highlighting and copy buttons.
      renderMarkdown(ai.answer, answer);
      enhanceCodeBlocks(ai.answer);
      chatFollow.changed();
    }
    if (errorText) {
      if (!answer) ai.wrapper.remove();
      appendBubble('error').answer.textContent = errorText;
      // The server saves nothing when generation fails: a brand-new
      // conversation therefore does not exist in the database.
      if (wasNew) currentConversationId = null;
    } else if (!answer) {
      ai.answer.textContent = '(Empty response)';
    }

    setBusy(false);
    userInput.focus();
    refreshConversationList();
  }

  chatForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const message = userInput.value.trim();
    if (!message || isGenerating) return;
    userInput.value = '';
    autoResize();
    sendMessage(message);
  });

  // Enter = send, Shift+Enter = new line (isComposing: ignore while an IME
  // is composing characters, e.g. accents on some keyboards).
  userInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      chatForm.requestSubmit();
    }
  });

  // Grow the textarea with its content, up to MAX_INPUT_HEIGHT.
  const MAX_INPUT_HEIGHT = 192; // px, same as max-height in css/app.css
  function autoResize() {
    userInput.style.height = 'auto';
    userInput.style.height = `${Math.min(userInput.scrollHeight, MAX_INPUT_HEIGHT)}px`;
    // Only show a scrollbar once the text no longer fits.
    userInput.style.overflowY = userInput.scrollHeight > MAX_INPUT_HEIGHT ? 'auto' : 'hidden';
  }
  userInput.addEventListener('input', autoResize);

  // --------------------------------------------------------------- health
  async function checkHealth() {
    const dot = $('status-dot');
    try {
      const h = await (await fetch('/api/health')).json();
      const ok = h.ollama === 'up' && h.database === 'up';
      dot.classList.toggle('is-up', ok);
      dot.classList.toggle('is-down', !ok);
      dot.setAttribute('aria-label', `AI engine ${h.ollama}, database ${h.database}`);
    } catch {
      dot.classList.remove('is-up');
      dot.classList.add('is-down');
      dot.setAttribute('aria-label', 'Server unreachable');
    }
  }

  // ----------------------------------------------------------------- init
  showWelcome();
  autoResize();
  loadModels();
  refreshConversationList();
  checkHealth();
  setInterval(checkHealth, 30000);
})();
