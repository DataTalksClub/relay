(() => {
  const root = document.querySelector('[data-campaign-composer]');
  if (!root) return;
  const mode = document.getElementById('id_editor_mode');
  const state = document.getElementById('id_editor_blocks');
  const panel = root.querySelector('[data-composer-panel]');
  const source = document.querySelector('[data-composer-source]');
  const list = root.querySelector('[data-composer-blocks]');
  const preview = root.querySelector('[data-composer-preview]');
  const announce = root.querySelector('[data-composer-announcement]');
  const names = {heading: 'Heading', paragraph: 'Paragraph', button: 'Button link', divider: 'Divider'};
  const escape = text => String(text || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let blocks;
  try { blocks = JSON.parse(state.value || '[]'); } catch { blocks = []; }
  if (!Array.isArray(blocks)) blocks = [];
  blocks = blocks.filter(block => block && Object.hasOwn(names, block.type));
  let nextId = 0;
  blocks.forEach(block => block.editorId = nextId++);
  function changed(notify = true) {
    state.value = JSON.stringify(blocks.map(({editorId, ...block}) => block));
    if (notify) state.dispatchEvent(new Event('input', {bubbles: true}));
    const body = blocks.map(block => {
      const text = escape(block.text).replace(/\n/g, '<br>');
      if (block.type === 'heading') return `<h1 style="font-size:24px;line-height:1.3;margin:0 0 20px">${text}</h1>`;
      if (block.type === 'paragraph') return `<p style="margin:0 0 20px;line-height:1.6">${text}</p>`;
      if (block.type === 'divider') return '<hr style="border:0;border-top:1px solid #d0d7de;margin:24px 0">';
      let url = '';
      try { const parsed = new URL(block.url); if (['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password) url = parsed.href; } catch {}
      return `<p style="margin:24px 0"><a ${url ? `href="${escape(url)}"` : ''} style="display:inline-block;background:#1f5c94;color:white;padding:12px 20px;text-decoration:none;border-radius:4px">${text || 'Button label'}</a></p>`;
    }).join('');
    preview.srcdoc = '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;background:white;color:#1f2328;font-family:Arial,sans-serif"><div style="max-width:600px;margin:0 auto;padding:32px 20px">' + body + '</div></body></html>';
    root.querySelector('[data-composer-empty]').hidden = blocks.length > 0;
    root.querySelectorAll('[data-add-block]').forEach(button => button.disabled = blocks.length >= 40);
  }
  function render(focusId, notify = true) {
    list.replaceChildren();
    blocks.forEach((block, index) => {
      const section = document.createElement('fieldset');
      section.className = 'section';
      const legend = document.createElement('legend');
      legend.textContent = `${index + 1}. ${names[block.type]}`;
      section.append(legend);
      const controls = document.createElement('div');
      controls.className = 'actions';
      [['Move up', -1], ['Move down', 1], ['Remove', 0]].forEach(([name, direction]) => {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'secondary'; button.textContent = name;
        button.setAttribute('aria-label', `${name}: ${names[block.type]} ${index + 1}`);
        button.disabled = direction && (index + direction < 0 || index + direction >= blocks.length);
        button.addEventListener('click', () => {
          if (!direction) blocks.splice(index, 1);
          else [blocks[index], blocks[index + direction]] = [blocks[index + direction], blocks[index]];
          render(direction ? block.editorId : blocks[Math.min(index, blocks.length - 1)]?.editorId);
          announce.textContent = direction ? `Block moved to position ${index + direction + 1}.` : 'Block removed.';
        });
        controls.append(button);
      });
      section.append(controls);
      if (block.type !== 'divider') {
        const label = document.createElement('label');
        const input = document.createElement(block.type === 'paragraph' ? 'textarea' : 'input');
        input.id = `composer-text-${block.editorId}`; input.value = block.text || '';
        input.maxLength = 10000;
        if (block.type === 'paragraph') input.rows = 4;
        label.htmlFor = input.id; label.textContent = block.type === 'button' ? 'Button label' : names[block.type];
        input.addEventListener('input', () => { block.text = input.value; changed(); });
        section.append(label, input);
        if (block.type === 'button') {
          const urlLabel = document.createElement('label'); const url = document.createElement('input');
          url.type = 'url'; url.id = `composer-url-${block.editorId}`; url.maxLength = 2000;
          url.placeholder = 'https://example.com'; url.value = block.url || '';
          urlLabel.htmlFor = url.id; urlLabel.textContent = 'Button destination URL';
          url.addEventListener('input', () => { block.url = url.value; changed(); });
          section.append(urlLabel, url);
        }
      }
      list.append(section);
    });
    changed(notify);
    if (focusId !== undefined) (document.getElementById(`composer-text-${focusId}`) || list.querySelector('button') || root.querySelector('[data-add-block]')).focus();
  }
  function selectMode(value, dirty = true) {
    mode.value = value;
    panel.hidden = value !== 'blocks'; source.hidden = value === 'blocks';
    root.querySelectorAll('[data-editor-mode]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.editorMode === value)));
    if (dirty) mode.dispatchEvent(new Event('input', {bubbles:true}));
  }
  root.querySelectorAll('[data-editor-mode]').forEach(button => button.addEventListener('click', () => selectMode(button.dataset.editorMode)));
  root.querySelectorAll('[data-add-block]').forEach(button => button.addEventListener('click', () => {
    const block = {type: button.dataset.addBlock, text:'', editorId:nextId++};
    if (block.type === 'button') block.url = '';
    blocks.push(block); render(block.editorId); announce.textContent = `${names[block.type]} added.`;
  }));
  root.querySelectorAll('[data-composer-width]').forEach(button => button.addEventListener('click', () => {
    preview.style.width = button.dataset.composerWidth; preview.style.maxWidth = '100%';
    root.querySelectorAll('[data-composer-width]').forEach(other => other.setAttribute('aria-pressed', String(other === button)));
  }));
  // Initialize without firing a dirty-form event on page load.
  const initial = state.value;
  render(undefined, false); state.value = initial;
  selectMode(mode.value === 'blocks' ? 'blocks' : 'source', false);
})();
