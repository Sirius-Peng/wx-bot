/* In-page confirmation: Electron's blocking window.confirm can lose keyboard focus
 * on Windows. A DOM dialog keeps focus inside the renderer, including with IME. */
'use strict';
let activeConfirmation = null;

async function confirmAction(message) {
  // Never let two unrelated operations share an affirmative response.
  if (activeConfirmation) return false;
  const dialog = document.getElementById('action-confirm');
  const cancel = document.getElementById('action-confirm-cancel');
  const accept = document.getElementById('action-confirm-accept');
  const previous = document.activeElement;
  document.getElementById('action-confirm-message').textContent = String(message);
  return new Promise(resolve => {
    const finish = result => {
      if (activeConfirmation?.finish !== finish) return;
      activeConfirmation = null;
      cancel.removeEventListener('click', onCancel);
      accept.removeEventListener('click', onAccept);
      dialog.removeEventListener('cancel', onNativeCancel);
      dialog.removeEventListener('close', onClose);
      document.removeEventListener('keydown', onKey, true);
      dialog.close();
      if (previous?.isConnected && !previous.disabled && previous.getClientRects().length) {
        previous.focus({ preventScroll: true });
      }
      resolve(result);
    };
    const onCancel = () => finish(false);
    const onAccept = () => finish(true);
    const onNativeCancel = e => { e.preventDefault(); finish(false); };
    // close events are queued. A stale event from the previous dialog must not
    // cancel a newly opened confirmation in the same task/microtask sequence.
    const onClose = () => { if (!dialog.open) finish(false); };
    const onKey = e => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopImmediatePropagation(); // Don't also dismiss the memory/QQ dialog underneath.
        finish(false);
      }
    };
    activeConfirmation = { finish };
    cancel.addEventListener('click', onCancel);
    accept.addEventListener('click', onAccept);
    dialog.addEventListener('cancel', onNativeCancel);
    dialog.addEventListener('close', onClose);
    document.addEventListener('keydown', onKey, true);
    try {
      dialog.showModal(); // Native DOM focus trap + inert background, not an OS message box.
      cancel.focus({ preventScroll: true });
    } catch (error) {
      finish(false);
      console.error('Confirmation dialog failed', error);
    }
  });
}
