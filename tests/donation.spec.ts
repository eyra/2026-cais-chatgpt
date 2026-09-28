import { test, expect, Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import * as path from 'path';

// Donated timestamps use the participant's local timezone, not UTC.
test.use({ timezoneId: 'Europe/Amsterdam' });

// Pyodide runs in a dedicated Worker, outside Playwright's page.clock. Shift
// the historical fixture relative to today's UTC day instead of freezing only
// the page: both messages stay in-window without depending on future dates.
const day = 86_400_000;
const referenceDay = Math.floor(Date.now() / day) * day;
const fixtureShift = (referenceDay - Date.parse('2026-09-23T00:00:00Z')) / 1000;
const newestTimestamp = Date.parse('2026-09-20T12:00:00Z') / 1000 + fixtureShift;
const questionTimestamp = Date.parse('2026-09-01T12:00:00Z') / 1000 + fixtureShift;
const fixtureConversations: unknown[] = JSON.parse(
  execFileSync('python3', ['-c',
    'import sys, zipfile; sys.stdout.buffer.write(zipfile.ZipFile(sys.argv[1]).read("conversations.json"))',
    path.join(__dirname, 'test.zip'),
  ]).toString(),
  (key, value) => key === 'create_time' && typeof value === 'number' ? value + fixtureShift : value,
);

interface ExportFile {
  name: string;
  mimeType: string;
  buffer: Buffer;
}

function exportFile(conversations: unknown[]): ExportFile {
  // Standard-library ZIP generation avoids extra dependencies and binary fixtures.
  const buffer = execFileSync('python3', ['-c', [
    'import io, sys, zipfile',
    'buffer = io.BytesIO()',
    'with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:',
    '    archive.writestr("conversations.json", sys.stdin.buffer.read())',
    'sys.stdout.buffer.write(buffer.getvalue())',
  ].join('\n')], { input: JSON.stringify(conversations) });
  return { name: 'chatgpt-export.zip', mimeType: 'application/zip', buffer };
}

function localTime(timestamp: number): string {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/Amsterdam', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(timestamp * 1000));
  const part = (name: string) => parts.find(value => value.type === name)!.value;
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}`;
}

const newestRow = {
  'conversation title': 'Research conversation', role: 'assistant',
  message: 'Newest answer', model: 'gpt-5', time: localTime(newestTimestamp),
};
const questionRow = {
  'conversation title': 'Research conversation', role: 'user',
  message: 'Participant question', model: '', time: localTime(questionTimestamp),
};

function messageNode(text: string, timestamp: unknown, content: unknown = { parts: [text] }) {
  return {
    message: {
      author: { role: 'assistant' }, create_time: timestamp,
      content, metadata: { model_slug: 'gpt-5' },
    },
  };
}

const secretText = 'SECRET skipped message';
const secretTitle = 'SECRET conversation title';

interface Donation {
  key: string;
  data: string;
}

// Records every donation, including the "<session>-tracking" log donations
// sent throughout the flow. Registered before the app loads.
async function openDonation(page: Page): Promise<Donation[]> {
  const donations: Donation[] = [];
  await page.route('/data-submission', async route => {
    donations.push(JSON.parse(route.request().postData()!));
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('http://localhost:3000/');
  await expect(page.getByRole('heading', { name: 'Your ChatGPT data' })).toBeVisible({ timeout: 90000 });
  return donations;
}

async function chooseExport(page: Page, file: ExportFile): Promise<void> {
  const fileChooserPromise = page.waitForEvent('filechooser');
  await page.getByText('Choose file').click();
  const fileChooser = await fileChooserPromise;
  await fileChooser.setFiles(file);
  await page.getByText('Continue').click();
}

async function uploadChatGPTExport(page: Page, conversations = fixtureConversations): Promise<Donation[]> {
  const donations = await openDonation(page);
  await chooseExport(page, exportFile(conversations));
  await expect(page.getByTestId('table-chatgpt_conversations_1')).toBeVisible();
  return donations;
}

function latestTracking(donations: Donation[]): string {
  const tracking = donations.filter(donation => donation.key.endsWith('-tracking')).at(-1);
  return tracking ? (JSON.parse(tracking.data) as string[]).join('\n') : '';
}

interface Submitted {
  tables: Record<string, { data: Record<string, string>[] }>;
  tracking: string;
  raw: string;
}

async function submitDonation(page: Page, donations: Donation[]): Promise<Submitted> {
  await page.getByText('Yes, donate', { exact: true }).click();
  // The flow donates the conversations, then the final tracking log.
  await expect.poll(() => latestTracking(donations)).toContain('Data donated');
  const conversations = donations.filter(donation => donation.key.endsWith('-chatgpt-conversations'));
  expect(conversations).toHaveLength(1);
  return {
    tables: JSON.parse(conversations[0].data),
    tracking: latestTracking(donations),
    raw: JSON.stringify(donations),
  };
}

test('reviews and submits visible ChatGPT messages from the export', async ({ page }) => {
  const oldTimestamp = Date.parse('2020-01-01T12:00:00Z') / 1000;
  const donations = await uploadChatGPTExport(page, [
    ...fixtureConversations,
    { title: 'Old conversation', mapping: { old: messageNode('Old message', oldTimestamp) } },
  ]);

  const consentTable = page.locator('div.mb-20', {
    has: page.getByTestId('table-chatgpt_conversations_1'),
  });
  await expect(consentTable.locator(':scope > .text-bodymedium')).toBeVisible();
  const table = page.getByTestId('table-chatgpt_conversations_1');
  // Labels are display-only: donated keys below stay lowercase.
  await expect(table.getByRole('columnheader', { name: 'Message', exact: true })).toBeVisible();
  await expect(table.getByText('Newest answer')).toBeVisible();
  await expect(table.getByText('Participant question')).toBeVisible();
  await expect(table.getByText('Old message', { exact: true })).toBeVisible();
  await expect(table.getByText('Hidden answer')).not.toBeVisible();

  const { tables, tracking } = await submitDonation(page, donations);
  expect(Object.keys(tables)).toEqual(['chatgpt_conversations_1']);
  expect(tables.chatgpt_conversations_1.data).toEqual([
    newestRow, questionRow,
    { 'conversation title': 'Old conversation', role: 'assistant', message: 'Old message', model: 'gpt-5', time: localTime(oldTimestamp) },
  ]);
  expect(tracking).not.toContain('Processing issues');
});

test('removes selected ChatGPT messages before donation', async ({ page }) => {
  const donations = await uploadChatGPTExport(page);

  await page.getByRole('checkbox').first().click();
  const table = page.getByTestId('table-chatgpt_conversations_1');
  await table.getByRole('checkbox').nth(1).click();
  await page.getByText('Delete selected').first().click();
  await expect(table.getByText('Newest answer')).not.toBeVisible();

  const { tables } = await submitDonation(page, donations);
  expect(tables.chatgpt_conversations_1.data).toEqual([questionRow]);
});

test('preserves Unicode and control characters through review and donation', async ({ page }) => {
  const text = '\uFEFFcafé — 中文 — \u{1D11E}\n"quoted" \\ \u0000';
  const donations = await uploadChatGPTExport(page, [{
    title: 'Überprüfung',
    mapping: { unicode: messageNode(text, newestTimestamp) },
  }]);
  await expect(page.getByTestId('table-chatgpt_conversations_1').getByText('café — 中文', { exact: false })).toBeVisible();

  const { tables } = await submitDonation(page, donations);
  expect(tables.chatgpt_conversations_1.data).toEqual([{
    'conversation title': 'Überprüfung',
    role: 'assistant',
    message: text,
    model: 'gpt-5',
    time: localTime(newestTimestamp),
  }]);
});

test('skips unusable messages and reports them only in the tracking donation', async ({ page }) => {
  const donations = await uploadChatGPTExport(page, [{
    title: secretTitle,
    mapping: {
      valid: messageNode('Newest answer', newestTimestamp),
      canvas: messageNode(secretText, newestTimestamp, { content_type: 'canvas', text: secretText }),
      future: messageNode(secretText, (referenceDay + 7 * day) / 1000),
    },
  }]);

  await expect(page.getByTestId('table-chatgpt_processing_issues_1')).not.toBeVisible();
  await expect(page.getByText(secretText)).not.toBeVisible();

  const { tables, tracking, raw } = await submitDonation(page, donations);
  expect(Object.keys(tables)).toEqual(['chatgpt_conversations_1']);
  expect(tables.chatgpt_conversations_1.data).toEqual([{ ...newestRow, 'conversation title': secretTitle }]);
  expect(tracking).toContain('Processing issues: ');
  expect(tracking).toContain('unknown_content_type:canvas message_excluded=1');
  expect(tracking).toContain('future_timestamp message_excluded=1');
  expect(raw).not.toContain(secretText);
});

test('still asks to donate an empty export', async ({ page }) => {
  const donations = await openDonation(page);
  await chooseExport(page, exportFile([{ title: secretTitle, mapping: {} }]));

  const { tables, raw } = await submitDonation(page, donations);
  expect(Object.keys(tables)).toEqual(['chatgpt_conversations_1']);
  expect(tables.chatgpt_conversations_1.data).toEqual([]);
  expect(raw).not.toContain('SECRET');
});

test('rejects a changed export format at once and tracks the reason', async ({ page }) => {
  const donations = await openDonation(page);
  await chooseExport(page, exportFile([
    { title: secretTitle, mapping: { valid: messageNode('Newest answer', newestTimestamp) } },
    { title: secretTitle, mapping: { changed: messageNode(secretText, '2026-09-20T12:00:00Z') } },
  ]));

  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
  await expect(page.getByText('Yes, donate', { exact: true })).not.toBeVisible();
  await expect.poll(() => latestTracking(donations))
    .toContain('unsupported format, reason=invalid_timestamp conversation=2 message=1');
  expect(JSON.stringify(donations)).not.toContain('SECRET');

  await page.getByRole('button', { name: 'Try again' }).click();
  await chooseExport(page, exportFile(fixtureConversations));
  await expect(page.getByTestId('table-chatgpt_conversations_1')).toBeVisible();

  const { tables } = await submitDonation(page, donations);
  expect(Object.keys(tables)).toEqual(['chatgpt_conversations_1']);
  expect(tables.chatgpt_conversations_1.data).toEqual([newestRow, questionRow]);
});

test('rejects an invalid ZIP and returns to file selection', async ({ page }) => {
  await openDonation(page);
  await chooseExport(page, {
    name: 'not-chatgpt.zip', mimeType: 'application/zip', buffer: Buffer.from('not a ZIP archive'),
  });

  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
  await expect(page.getByText('Yes, donate', { exact: true })).not.toBeVisible();
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByText('Choose file')).toBeVisible();
});
