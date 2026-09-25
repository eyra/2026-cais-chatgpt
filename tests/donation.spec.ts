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

function messageNode(text: string, timestamp: unknown) {
  return {
    message: {
      author: { role: 'assistant' }, create_time: timestamp,
      content: { parts: [text] }, metadata: { model_slug: 'gpt-5' },
    },
  };
}

const failedText = 'SECRET invalid timestamp message';
const failedTitle = 'SECRET invalid conversation title';
const failedMappingText = 'SECRET invalid mapping text';
const invalidConversation = { title: failedTitle, mapping: [failedMappingText] };
const invalidMessageIssue = {
  conversation: '1', message: '2', reason: 'invalid_timestamp', action: 'message_excluded',
};
const invalidConversationIssue = {
  conversation: '2', message: '', reason: 'invalid_mapping', action: 'conversation_excluded',
};

const mixedConversations: unknown[] = [
  {
    title: 'Research conversation',
    mapping: {
      valid: messageNode('Newest answer', newestTimestamp),
      invalid: messageNode(failedText, 'not a timestamp'),
    },
  },
  invalidConversation,
];

async function openDonation(page: Page): Promise<void> {
  await page.goto('http://localhost:3000/');
  await expect(page.getByRole('heading', { name: 'Your ChatGPT data' })).toBeVisible({ timeout: 90000 });
}

async function chooseExport(page: Page, file: ExportFile): Promise<void> {
  const fileChooserPromise = page.waitForEvent('filechooser');
  await page.getByText('Choose file').click();
  const fileChooser = await fileChooserPromise;
  await fileChooser.setFiles(file);
  await page.getByText('Continue').click();
}

async function uploadChatGPTExport(page: Page, conversations = fixtureConversations): Promise<void> {
  await openDonation(page);
  await chooseExport(page, exportFile(conversations));
  await expect(page.getByTestId('table-chatgpt_conversations_1')).toBeVisible();
}

function captureDonation(page: Page): Promise<string | null> {
  const { promise, resolve } = Promise.withResolvers<string | null>();
  page.route('/data-submission', async route => {
    await route.fulfill({ json: { ok: true } });
    resolve(route.request().postData());
  });
  return promise;
}

function donatedTables(submittedData: string): Record<string, { data: Record<string, string>[] }> {
  const request = JSON.parse(submittedData);
  return JSON.parse(request.data);
}

async function submitDonation(page: Page): Promise<string> {
  const donation = captureDonation(page);
  await page.getByText('Yes, donate', { exact: true }).click();
  const submittedData = await donation;
  expect(submittedData).not.toBeNull();
  return submittedData!;
}

test('reviews and submits visible ChatGPT messages from the export', async ({ page }) => {
  await uploadChatGPTExport(page);

  const table = page.getByTestId('table-chatgpt_conversations_1');
  // Labels are display-only: donated keys below stay lowercase.
  await expect(table.getByRole('columnheader', { name: 'Message', exact: true })).toBeVisible();
  await expect(table.getByText('Newest answer')).toBeVisible();
  await expect(table.getByText('Participant question')).toBeVisible();
  await expect(table.getByText('Hidden answer')).not.toBeVisible();
  await expect(page.getByTestId('table-chatgpt_processing_issues_1')).not.toBeVisible();

  const tables = donatedTables(await submitDonation(page));
  expect(Object.keys(tables)).toEqual(['chatgpt_conversations_1']);
  expect(tables.chatgpt_conversations_1.data).toEqual([newestRow, questionRow]);
});

test('removes selected ChatGPT messages before donation', async ({ page }) => {
  await uploadChatGPTExport(page);

  await page.getByRole('checkbox').first().click();
  const table = page.getByTestId('table-chatgpt_conversations_1');
  await table.getByRole('checkbox').nth(1).click();
  await page.getByText('Delete selected').first().click();
  await expect(table.getByText('Newest answer')).not.toBeVisible();

  const tables = donatedTables(await submitDonation(page));
  expect(tables.chatgpt_conversations_1.data).toEqual([questionRow]);
});

test('reviews and donates privacy-safe processing issues alongside retained messages', async ({ page }) => {
  await uploadChatGPTExport(page, mixedConversations);

  const issues = page.getByTestId('table-chatgpt_processing_issues_1');
  await expect(issues).toBeVisible();
  await expect(issues.getByText('invalid_timestamp', { exact: true })).toBeVisible();
  await expect(issues.getByText('invalid_mapping', { exact: true })).toBeVisible();
  for (const secret of [failedText, failedTitle, failedMappingText]) {
    await expect(page.getByText(secret, { exact: true })).not.toBeVisible();
  }

  const submittedData = await submitDonation(page);
  const tables = donatedTables(submittedData);
  expect(Object.keys(tables).sort()).toEqual(['chatgpt_conversations_1', 'chatgpt_processing_issues_1']);
  expect(tables.chatgpt_conversations_1.data).toEqual([newestRow]);
  expect(tables.chatgpt_processing_issues_1.data).toEqual([invalidMessageIssue, invalidConversationIssue]);
  for (const secret of [failedText, failedTitle, failedMappingText]) {
    expect(submittedData).not.toContain(secret);
  }
});

test('lets participants remove processing issue rows before donation', async ({ page }) => {
  await uploadChatGPTExport(page, mixedConversations);

  const issues = page.getByTestId('table-chatgpt_processing_issues_1');
  await expect(issues).toBeVisible();
  // Before editing there is one Adjust checkbox per table.
  await page.getByRole('checkbox').nth(1).click();
  await issues.getByRole('checkbox').nth(1).click();
  await page.getByText('Delete selected', { exact: true }).nth(1).click();
  await expect(issues.getByText('invalid_timestamp', { exact: true })).not.toBeVisible();
  await expect(issues.getByText('invalid_mapping', { exact: true })).toBeVisible();

  const tables = donatedTables(await submitDonation(page));
  expect(tables.chatgpt_conversations_1.data).toEqual([newestRow]);
  expect(tables.chatgpt_processing_issues_1.data).toEqual([invalidConversationIssue]);
});

test('excludes future messages and donates their processing issue', async ({ page }) => {
  const futureText = 'SECRET future message';
  await uploadChatGPTExport(page, [{
    title: 'Research conversation',
    mapping: {
      valid: messageNode('Newest answer', newestTimestamp),
      future: messageNode(futureText, (referenceDay + 7 * day) / 1000),
    },
  }]);

  const issues = page.getByTestId('table-chatgpt_processing_issues_1');
  await expect(issues.getByText('future_timestamp', { exact: true })).toBeVisible();
  const submittedData = await submitDonation(page);
  const tables = donatedTables(submittedData);
  expect(tables.chatgpt_conversations_1.data).toEqual([newestRow]);
  expect(tables.chatgpt_processing_issues_1.data).toEqual([{
    conversation: '1', message: '2', reason: 'future_timestamp', action: 'message_excluded',
  }]);
  expect(submittedData).not.toContain(futureText);
});

test('offers retry rather than issues-only donation when every record fails', async ({ page }) => {
  await openDonation(page);
  await chooseExport(page, exportFile([
    {
      title: failedTitle,
      mapping: { invalid: messageNode(failedText, 'not a timestamp') },
    },
    invalidConversation,
  ]));

  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
  await expect(page.getByText('Yes, donate', { exact: true })).not.toBeVisible();
  await expect(page.getByTestId('table-chatgpt_processing_issues_1')).not.toBeVisible();
  await page.getByRole('button', { name: 'Try again' }).click();
  await chooseExport(page, exportFile(fixtureConversations));
  await expect(page.getByTestId('table-chatgpt_conversations_1')).toBeVisible();

  const tables = donatedTables(await submitDonation(page));
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
