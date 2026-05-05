/**
 * Reusable modal with backdrop blur.
 * Matches CRM modal pattern.
 */

let activeModal = null;

export function openModal({ title, content, onClose }) {
  closeModal();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';

  const panel = document.createElement('div');
  panel.className = 'modal-panel';

  if (title) {
    const h2 = document.createElement('h2');
    h2.textContent = title;
    panel.appendChild(h2);
  }

  if (typeof content === 'string') {
    const div = document.createElement('div');
    div.innerHTML = content;
    panel.appendChild(div);
  } else if (content instanceof HTMLElement) {
    panel.appendChild(content);
  }

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeModal();
  });

  document.addEventListener('keydown', handleEsc);
  document.body.appendChild(overlay);
  document.body.appendChild(panel);

  activeModal = { overlay, panel, onClose };
  return panel;
}

export function closeModal() {
  if (!activeModal) return;
  activeModal.overlay.remove();
  activeModal.panel.remove();
  if (activeModal.onClose) activeModal.onClose();
  document.removeEventListener('keydown', handleEsc);
  activeModal = null;
}

function handleEsc(e) {
  if (e.key === 'Escape') closeModal();
}
