import assert from 'node:assert/strict';
import { canConfirmConsent, emptyConsent, lessonConfirmation, coverConfirmation } from '../frontend/src/lib/consent.ts';
const service = { config_id: 'model-1', config_version: 7, model_name: 'Synthetic', protocol: 'openai', service_endpoint: 'https://synthetic.invalid/v1' };
const choices = emptyConsent();
assert.deepEqual(choices, { content: false, tts: false, prewarm: false, courses: false, configId: '' });
assert.equal(canConfirmConsent({ content: service }, choices), false);
assert.equal(canConfirmConsent({ content: null }, { ...choices, content: true }), false);
assert.equal(canConfirmConsent({ content: service }, { ...choices, content: true }), true);
assert.equal(canConfirmConsent({ content: service, purpose: 'prewarm' }, { ...choices, content: true }), false);
assert.equal(canConfirmConsent({ content: service, purpose: 'prewarm' }, { ...choices, content: true, prewarm: true }), true);
assert.equal(canConfirmConsent({ configs: [service], content: service }, { ...choices, content: true, configId: 'missing' }), false);
assert.throws(() => lessonConfirmation('f'.repeat(64), 'start', choices), /Explicit consent/);
assert.throws(() => lessonConfirmation('f'.repeat(64), 'prewarm', { ...choices, content: true }), /Explicit consent/);
assert.deepEqual(lessonConfirmation('f'.repeat(64), 'recover', { ...choices, content: true }, 'conv-1'), {
  fingerprint: 'f'.repeat(64), purpose: 'recover', conversation_id: 'conv-1', content_confirmed: true, tts_confirmed: false, prewarm_opt_in: false,
});
const preflight = { config: service, configs: [service], title_hash: 't'.repeat(64), course_topics: [{ title: 'Course', title_hash: 'c'.repeat(64) }] };
assert.throws(() => coverConfirmation(preflight, choices, 'key'), /Explicit consent/);
assert.throws(() => coverConfirmation(preflight, { ...choices, content: true, configId: 'missing' }, 'key'), /Explicit consent/);
const exact = coverConfirmation(preflight, { ...choices, content: true, configId: service.config_id }, 'key');
assert.equal(exact.config_version, 7);
assert.equal(exact.authorization_scope, 'exact_target');
assert.deepEqual(exact.course_topic_hashes, []);
const broader = coverConfirmation(preflight, { ...choices, content: true, courses: true, configId: service.config_id }, 'key');
assert.equal(broader.authorization_scope, 'exact_target');
assert.deepEqual(broader.course_topic_hashes, []);
assert.equal(broader.title_hash, preflight.title_hash);
console.log('consent_unit: 17 assertions passed');
