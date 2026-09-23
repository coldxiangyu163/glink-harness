/**
 * Minimal DOM stub for exercising the workbench shell in Node.
 *
 * `workspace.mjs` is a browser module: it wires one document, one viewport and
 * a handful of observers. The navigation/lifecycle regressions need that wiring
 * without a browser and without a layout engine — this file provides exactly the
 * surface the shell touches, plus a switch to resolve `getBoundingClientRect`
 * to the two-column geometry. It is deliberately permissive (unknown selectors
 * return `null`), so it never pretends to be a full DOM; it only refuses to
 * crash on APIs the shell actually uses.
 */

function makeClassList() {
  const values = new Set();
  return {
    add(...names) {
      for (const name of names) values.add(name);
    },
    remove(...names) {
      for (const name of names) values.delete(name);
    },
    toggle(name, force) {
      const on = force === undefined ? !values.has(name) : Boolean(force);
      if (on) values.add(name);
      else values.delete(name);
      return on;
    },
    contains(name) {
      return values.has(name);
    },
    get value() {
      return [...values].join(' ');
    },
  };
}

export function createDomStub({
  width = 1280,
  height = 900,
  location: initialLocation = { pathname: '/', search: '', origin: 'http://127.0.0.1:43210' },
  hidden = false,
} = {}) {
  const byId = new Map();
  const observers = [];
  const documentListeners = new Map();
  const windowListeners = new Map();
  const storage = new Map();
  const raf = [];

  class StubObserver {
    constructor(callback) {
      this.callback = callback;
      this.targets = [];
      this.disconnected = false;
      observers.push(this);
    }
    observe(target) {
      this.targets.push(target);
    }
    unobserve(target) {
      this.targets = this.targets.filter((item) => item !== target);
    }
    disconnect() {
      this.disconnected = true;
      this.targets = [];
    }
    takeRecords() {
      return [];
    }
  }

  function makeElement(tag = 'div') {
    const listeners = new Map();
    const element = {
      tagName: String(tag).toUpperCase(),
      children: [],
      dataset: {},
      style: {
        setProperty() {},
        removeProperty() {},
        getPropertyValue() {
          return '';
        },
      },
      classList: makeClassList(),
      className: '',
      id: '',
      hidden: false,
      inert: false,
      disabled: false,
      readOnly: false,
      value: '',
      textContent: '',
      innerHTML: '',
      title: '',
      type: '',
      href: '',
      download: '',
      src: '',
      alt: '',
      tabIndex: 0,
      scrollTop: 0,
      scrollHeight: 1000,
      offsetHeight: 68,
      clientWidth: width,
      clientHeight: height,
      parentElement: null,
      attributes: {},
      setAttribute(name, value) {
        this.attributes[name] = String(value);
      },
      getAttribute(name) {
        return name in this.attributes ? this.attributes[name] : null;
      },
      removeAttribute(name) {
        delete this.attributes[name];
      },
      toggleAttribute(name, force) {
        const on = force === undefined ? !(name in this.attributes) : Boolean(force);
        if (on) this.attributes[name] = '';
        else delete this.attributes[name];
        return on;
      },
      hasAttribute(name) {
        return name in this.attributes;
      },
      append(...nodes) {
        for (const node of nodes) this.appendChild(node);
      },
      appendChild(node) {
        if (node && typeof node === 'object') node.parentElement = this;
        this.children.push(node);
        return node;
      },
      replaceChildren(...nodes) {
        for (const node of nodes) if (node && typeof node === 'object') node.parentElement = this;
        this.children = nodes;
      },
      removeChild(node) {
        this.children = this.children.filter((item) => item !== node);
        return node;
      },
      remove() {
        if (this.parentElement) this.parentElement.removeChild(this);
      },
      querySelector() {
        return null;
      },
      querySelectorAll() {
        return [];
      },
      closest() {
        return null;
      },
      matches() {
        return false;
      },
      contains() {
        return false;
      },
      addEventListener(type, handler) {
        const set = listeners.get(type) ?? new Set();
        set.add(handler);
        listeners.set(type, set);
      },
      removeEventListener(type, handler) {
        listeners.get(type)?.delete(handler);
      },
      dispatch(type, event = {}) {
        for (const handler of listeners.get(type) ?? []) handler(event);
      },
      listenerCount(type) {
        return listeners.get(type)?.size ?? 0;
      },
      focus() {},
      blur() {},
      click() {},
      setPointerCapture() {},
      releasePointerCapture() {},
      getBoundingClientRect() {
        return { width, height, left: 0, top: 0, right: width, bottom: height, x: 0, y: 0 };
      },
    };
    return element;
  }

  function getElementById(id) {
    if (!byId.has(id)) {
      const element = makeElement('div');
      element.id = id;
      byId.set(id, element);
    }
    return byId.get(id);
  }

  const body = makeElement('body');
  const documentElement = makeElement('html');
  const location = {
    pathname: initialLocation.pathname ?? '/',
    search: initialLocation.search ?? '',
    origin: initialLocation.origin ?? 'http://127.0.0.1:43210',
    reloaded: false,
    reload() {
      location.reloaded = true;
    },
    assign() {},
  };
  const applyUrl = (url) => {
    try {
      const parsed = new URL(url, location.origin);
      location.pathname = parsed.pathname;
      location.search = parsed.search;
    } catch {
      /* the stub keeps the previous URL on unparseable input */
    }
  };
  const history = {
    state: null,
    replaceState(state, _title, url) {
      history.state = state;
      if (typeof url === 'string' && url) applyUrl(url);
    },
    pushState(state, _title, url) {
      history.state = state;
      if (typeof url === 'string' && url) applyUrl(url);
    },
  };
  const document = {
    body,
    documentElement,
    title: '',
    hidden,
    location,
    createElement: (tag) => makeElement(tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
    getElementById,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener(type, handler) {
      const set = documentListeners.get(type) ?? new Set();
      set.add(handler);
      documentListeners.set(type, set);
    },
    removeEventListener(type, handler) {
      documentListeners.get(type)?.delete(handler);
    },
    dispatch(type, event = {}) {
      for (const handler of documentListeners.get(type) ?? []) handler(event);
    },
  };

  const window = {
    innerWidth: width,
    innerHeight: height,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    addEventListener(type, handler) {
      const set = windowListeners.get(type) ?? new Set();
      set.add(handler);
      windowListeners.set(type, set);
    },
    removeEventListener(type, handler) {
      windowListeners.get(type)?.delete(handler);
    },
    dispatch(type, event = {}) {
      for (const handler of windowListeners.get(type) ?? []) handler(event);
    },
    localStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
      clear: () => storage.clear(),
    },
  };

  const globals = {
    document,
    window,
    location,
    history,
    navigator: { clipboard: { writeText: async () => {} } },
    localStorage: window.localStorage,
    performance: { now: () => Date.now() },
    requestAnimationFrame: (callback) => {
      raf.push(callback);
      return raf.length;
    },
    cancelAnimationFrame: () => {},
    MutationObserver: StubObserver,
    ResizeObserver: StubObserver,
    EventSource: class {
      constructor(url) {
        this.url = url;
        this.readyState = 0;
        this.listeners = new Map();
      }
      addEventListener(type, handler) {
        this.listeners.set(type, handler);
      }
      close() {
        this.readyState = 2;
      }
      emit(type, payload) {
        this.listeners.get(type)?.({ data: JSON.stringify(payload) });
      }
    },
  };

  const previous = new Map();
  for (const [name, value] of Object.entries(globals)) {
    previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, {
      value,
      configurable: true,
      writable: true,
      enumerable: true,
    });
  }

  return {
    document,
    window,
    body,
    location,
    history,
    observers,
    raf,
    getElement(id) {
      return byId.get(id);
    },
    install() {
      return this;
    },
    restore() {
      for (const [name, descriptor] of previous) {
        if (descriptor) Object.defineProperty(globalThis, name, descriptor);
        else delete globalThis[name];
      }
    },
  };
}
