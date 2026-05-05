/**
 * Simple pub/sub event bus for cross-module communication.
 */

const handlers = new Map();

export const events = {
  on(event, callback) {
    if (!handlers.has(event)) handlers.set(event, new Set());
    handlers.get(event).add(callback);
    return () => handlers.get(event).delete(callback);
  },

  off(event, callback) {
    handlers.get(event)?.delete(callback);
  },

  emit(event, data) {
    handlers.get(event)?.forEach(cb => cb(data));
  },

  once(event, callback) {
    const unsub = this.on(event, (data) => {
      unsub();
      callback(data);
    });
    return unsub;
  },
};
