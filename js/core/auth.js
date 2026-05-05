/**
 * Firebase Auth — shared with THT CRM.
 * Same Firebase project (tht-crm), same Firestore users collection.
 */

import { store } from './state.js';
import { setAuthToken } from './api.js';

const FIREBASE_CONFIG = {
  apiKey: "AIzaSyCbEwfKvC71Md5u2AIyKC_yyxdgluUZ-Jg",
  authDomain: "tht-crm.firebaseapp.com",
  projectId: "tht-crm",
  storageBucket: "tht-crm.firebasestorage.app",
  messagingSenderId: "1007498122498",
  appId: "1:1007498122498:web:cd2c1fb5ae09e28d7adf35",
};

let auth = null;
let db = null;

export function initAuth() {
  if (!window.firebase) {
    console.warn('Firebase SDK not loaded');
    store.set('authReady', true);
    return;
  }

  if (!firebase.apps.length) {
    firebase.initializeApp(FIREBASE_CONFIG);
  }
  auth = firebase.auth();
  db = firebase.firestore();

  auth.onAuthStateChanged(async (user) => {
    if (user) {
      const token = await user.getIdToken();
      setAuthToken(token);

      const doc = await db.collection('users').doc(user.uid).get();
      const userData = doc.exists ? doc.data() : {};

      store.set('user', {
        uid: user.uid,
        email: user.email,
        name: user.displayName || userData.name || '',
        role: userData.role || 'admin',
      });

      // Refresh token periodically
      setInterval(async () => {
        const newToken = await user.getIdToken(true);
        setAuthToken(newToken);
      }, 50 * 60 * 1000);
    } else {
      store.set('user', null);
      setAuthToken(null);
    }
    store.set('authReady', true);
  });
}

export async function login(email, password) {
  if (!auth) throw new Error('Auth not initialized');
  return auth.signInWithEmailAndPassword(email, password);
}

export async function logout() {
  if (!auth) return;
  await auth.signOut();
  store.set('user', null);
  setAuthToken(null);
}

export function getCurrentUser() {
  return store.get('user');
}
