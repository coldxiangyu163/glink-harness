// 生成文件，勿手改。来源：host/pi_glink/event_schema.py
// 重新生成：python scripts/gen-event-schema.py；scripts/check.sh 以 --check 校验它是最新的。

/**
 * run_started（v1）
 * @typedef {object} RunStartedPayload
 * @property {string} prompt
 */
/**
 * run_stage（v1）
 * @typedef {object} RunStagePayload
 * @property {"agent_start" | "agent_end" | "compaction_start" | "compaction_end" | "auto_retry_start" | "auto_retry_end" | "plan"} stage
 * @property {boolean} [willRetry]
 * @property {string} [reason]
 * @property {number | null} [attempt]
 * @property {string} [errorMessage]
 * @property {boolean} [success]
 * @property {Array<Record<string, unknown>>} [entries] ACP plan: {content, status, priority}
 */
/**
 * run_finished（v1）：权威终帧：status 与事件类型一一对应
 * @typedef {object} RunFinishedPayload
 * @property {"finished"} status
 * @property {string | null} stopReason
 * @property {string | null} error
 * @property {string | null} reportPath
 * @property {string} reportStatus
 * @property {Record<string, unknown>} cleanup {released, failures, external}
 * @property {Array<Record<string, unknown>>} children 子代理终态摘要（权威）
 * @property {Array<Record<string, unknown>>} [deliverables] 声明路径及逐文件回收结果
 * @property {string | null} [modelStopReason]
 * @property {string | null} [sessionId]
 * @property {string | null} [sessionFile]
 * @property {Record<string, unknown> | null} [usage]
 * @property {Record<string, unknown> | null} [kernelUsage]
 * @property {boolean} [redacted]
 * @property {number} [durationSeconds]
 * @property {Array<unknown>} [artifactFailures]
 * @property {Record<string, unknown>} [external] 遗留 run 收口时的外部资源事实
 */
/**
 * run_failed（v1）：权威终帧：status 与事件类型一一对应
 * @typedef {object} RunFailedPayload
 * @property {"failed"} status
 * @property {string | null} stopReason
 * @property {string | null} error
 * @property {string | null} reportPath
 * @property {string} reportStatus
 * @property {Record<string, unknown>} cleanup {released, failures, external}
 * @property {Array<Record<string, unknown>>} children 子代理终态摘要（权威）
 * @property {Array<Record<string, unknown>>} [deliverables] 声明路径及逐文件回收结果
 * @property {string | null} [modelStopReason]
 * @property {string | null} [sessionId]
 * @property {string | null} [sessionFile]
 * @property {Record<string, unknown> | null} [usage]
 * @property {Record<string, unknown> | null} [kernelUsage]
 * @property {boolean} [redacted]
 * @property {number} [durationSeconds]
 * @property {Array<unknown>} [artifactFailures]
 * @property {Record<string, unknown>} [external] 遗留 run 收口时的外部资源事实
 */
/**
 * run_cancelled（v1）：权威终帧：status 与事件类型一一对应
 * @typedef {object} RunCancelledPayload
 * @property {"cancelled"} status
 * @property {string | null} stopReason
 * @property {string | null} error
 * @property {string | null} reportPath
 * @property {string} reportStatus
 * @property {Record<string, unknown>} cleanup {released, failures, external}
 * @property {Array<Record<string, unknown>>} children 子代理终态摘要（权威）
 * @property {Array<Record<string, unknown>>} [deliverables] 声明路径及逐文件回收结果
 * @property {string | null} [modelStopReason]
 * @property {string | null} [sessionId]
 * @property {string | null} [sessionFile]
 * @property {Record<string, unknown> | null} [usage]
 * @property {Record<string, unknown> | null} [kernelUsage]
 * @property {boolean} [redacted]
 * @property {number} [durationSeconds]
 * @property {Array<unknown>} [artifactFailures]
 * @property {Record<string, unknown>} [external] 遗留 run 收口时的外部资源事实
 */
/**
 * session_ready（v1）：内核自报的会话事实，其余键按内核原样透传
 * @typedef {object} SessionReadyPayload
 * @property {string | null} [sessionId]
 * @property {string | null} sessionFile
 * @property {string | null} [model]
 * @property {string | null} [thinkingLevel]
 */
/**
 * turn_started（v1）
 * @typedef {object} TurnStartedPayload
 */
/**
 * turn_finished（v1）
 * @typedef {object} TurnFinishedPayload
 */
/**
 * assistant_delta（v1）：增量
 * @typedef {object} AssistantDeltaPayload
 * @property {string} text
 */
/**
 * thinking_delta（v1）：增量
 * @typedef {object} ThinkingDeltaPayload
 * @property {string} text
 */
/**
 * assistant_message（v1）：权威正文；truncated 时不覆盖更长的流式正文
 * @typedef {object} AssistantMessagePayload
 * @property {string} text
 * @property {boolean} [truncated]
 * @property {string} [stopReason]
 * @property {Record<string, unknown>} [usage]
 */
/**
 * deliverables_declared（v1）：ACP present 的声明，不等于文件已安全回收；终帧和 run 详情给出逐文件结果
 * @typedef {object} DeliverablesDeclaredPayload
 * @property {Array<Record<string, unknown>>} files 工作区相对路径与可选说明的声明列表
 */
/**
 * tool_started（v1）
 * @typedef {object} ToolStartedPayload
 * @property {string} toolCallId
 * @property {string} toolName
 * @property {Record<string, unknown>} args
 * @property {string} [kind]
 * @property {string} [presentation] 族投影的工具呈现提示；缺失时旧帧由读侧回退
 */
/**
 * tool_updated（v1）：output 是累计快照，替换而非拼接
 * @typedef {object} ToolUpdatedPayload
 * @property {string} toolCallId
 * @property {string} toolName
 * @property {string} output
 */
/**
 * tool_finished（v1）：工具终帧（权威）
 * @typedef {object} ToolFinishedPayload
 * @property {string} toolCallId
 * @property {string} toolName
 * @property {boolean} isError
 * @property {string} result
 */
/**
 * subagent_started（v1）
 * @typedef {object} SubagentStartedPayload
 * @property {number} n
 * @property {string} toolCallId
 * @property {string} agent
 * @property {string} task
 */
/**
 * subagent_finished（v1）
 * @typedef {object} SubagentFinishedPayload
 * @property {string} toolCallId
 * @property {Array<number>} ns
 * @property {"finished" | "failed"} status
 * @property {boolean} isError
 * @property {string} resultPreview
 */
/**
 * subagent_updated（v1）：运行中进度（累计快照，最新值为准）
 * @typedef {object} SubagentUpdatedPayload
 * @property {number} n
 * @property {number} steps
 * @property {string | null} lastToolName
 * @property {string | null} lastToolTarget
 * @property {number} bytes
 */
/**
 * usage_reported（v1）：usage 为累计值
 * @typedef {object} UsageReportedPayload
 * @property {Record<string, unknown>} usage
 * @property {Record<string, unknown>} [requestUsage]
 */
/**
 * kernel_status（v1）
 * @typedef {object} KernelStatusPayload
 * @property {"info" | "warning"} level
 * @property {string} message
 * @property {string} detail
 * @property {string} [extensionPath]
 * @property {string} [resumeMethod]
 * @property {string} [sessionId]
 * @property {number} [replayedUpdates]
 * @property {string} [decision]
 * @property {string} [policy]
 * @property {string} [toolCallId]
 * @property {string} [toolTitle]
 * @property {string} [toolKind]
 * @property {string} [toolKindSource]
 * @property {string} [optionId]
 * @property {string} [optionKind]
 * @property {Array<Record<string, unknown>>} [commands]
 */

/** 产品事件 schema：类型 → {v, terminal, open, fields}。 */
export const EVENT_SCHEMA = Object.freeze({
  "run_started": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "prompt",
        "types": [
          "string"
        ],
        "required": true
      }
    ]
  },
  "run_stage": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "stage",
        "types": [
          "string"
        ],
        "required": true,
        "enum": [
          "agent_start",
          "agent_end",
          "compaction_start",
          "compaction_end",
          "auto_retry_start",
          "auto_retry_end",
          "plan"
        ]
      },
      {
        "name": "willRetry",
        "types": [
          "boolean"
        ],
        "required": false
      },
      {
        "name": "reason",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "attempt",
        "types": [
          "integer",
          "null"
        ],
        "required": false
      },
      {
        "name": "errorMessage",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "success",
        "types": [
          "boolean"
        ],
        "required": false
      },
      {
        "name": "entries",
        "types": [
          "array"
        ],
        "required": false,
        "items": "object",
        "doc": "ACP plan: {content, status, priority}"
      }
    ]
  },
  "run_finished": {
    "v": 1,
    "terminal": true,
    "open": false,
    "fields": [
      {
        "name": "status",
        "types": [
          "string"
        ],
        "required": true,
        "enum": [
          "finished"
        ]
      },
      {
        "name": "stopReason",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "error",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "reportPath",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "reportStatus",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "cleanup",
        "types": [
          "object"
        ],
        "required": true,
        "doc": "{released, failures, external}"
      },
      {
        "name": "children",
        "types": [
          "array"
        ],
        "required": true,
        "items": "object",
        "doc": "子代理终态摘要（权威）"
      },
      {
        "name": "deliverables",
        "types": [
          "array"
        ],
        "required": false,
        "items": "object",
        "doc": "声明路径及逐文件回收结果"
      },
      {
        "name": "modelStopReason",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "sessionId",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "sessionFile",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "usage",
        "types": [
          "object",
          "null"
        ],
        "required": false
      },
      {
        "name": "kernelUsage",
        "types": [
          "object",
          "null"
        ],
        "required": false
      },
      {
        "name": "redacted",
        "types": [
          "boolean"
        ],
        "required": false
      },
      {
        "name": "durationSeconds",
        "types": [
          "number"
        ],
        "required": false
      },
      {
        "name": "artifactFailures",
        "types": [
          "array"
        ],
        "required": false
      },
      {
        "name": "external",
        "types": [
          "object"
        ],
        "required": false,
        "doc": "遗留 run 收口时的外部资源事实"
      }
    ]
  },
  "run_failed": {
    "v": 1,
    "terminal": true,
    "open": false,
    "fields": [
      {
        "name": "status",
        "types": [
          "string"
        ],
        "required": true,
        "enum": [
          "failed"
        ]
      },
      {
        "name": "stopReason",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "error",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "reportPath",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "reportStatus",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "cleanup",
        "types": [
          "object"
        ],
        "required": true,
        "doc": "{released, failures, external}"
      },
      {
        "name": "children",
        "types": [
          "array"
        ],
        "required": true,
        "items": "object",
        "doc": "子代理终态摘要（权威）"
      },
      {
        "name": "deliverables",
        "types": [
          "array"
        ],
        "required": false,
        "items": "object",
        "doc": "声明路径及逐文件回收结果"
      },
      {
        "name": "modelStopReason",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "sessionId",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "sessionFile",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "usage",
        "types": [
          "object",
          "null"
        ],
        "required": false
      },
      {
        "name": "kernelUsage",
        "types": [
          "object",
          "null"
        ],
        "required": false
      },
      {
        "name": "redacted",
        "types": [
          "boolean"
        ],
        "required": false
      },
      {
        "name": "durationSeconds",
        "types": [
          "number"
        ],
        "required": false
      },
      {
        "name": "artifactFailures",
        "types": [
          "array"
        ],
        "required": false
      },
      {
        "name": "external",
        "types": [
          "object"
        ],
        "required": false,
        "doc": "遗留 run 收口时的外部资源事实"
      }
    ]
  },
  "run_cancelled": {
    "v": 1,
    "terminal": true,
    "open": false,
    "fields": [
      {
        "name": "status",
        "types": [
          "string"
        ],
        "required": true,
        "enum": [
          "cancelled"
        ]
      },
      {
        "name": "stopReason",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "error",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "reportPath",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "reportStatus",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "cleanup",
        "types": [
          "object"
        ],
        "required": true,
        "doc": "{released, failures, external}"
      },
      {
        "name": "children",
        "types": [
          "array"
        ],
        "required": true,
        "items": "object",
        "doc": "子代理终态摘要（权威）"
      },
      {
        "name": "deliverables",
        "types": [
          "array"
        ],
        "required": false,
        "items": "object",
        "doc": "声明路径及逐文件回收结果"
      },
      {
        "name": "modelStopReason",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "sessionId",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "sessionFile",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "usage",
        "types": [
          "object",
          "null"
        ],
        "required": false
      },
      {
        "name": "kernelUsage",
        "types": [
          "object",
          "null"
        ],
        "required": false
      },
      {
        "name": "redacted",
        "types": [
          "boolean"
        ],
        "required": false
      },
      {
        "name": "durationSeconds",
        "types": [
          "number"
        ],
        "required": false
      },
      {
        "name": "artifactFailures",
        "types": [
          "array"
        ],
        "required": false
      },
      {
        "name": "external",
        "types": [
          "object"
        ],
        "required": false,
        "doc": "遗留 run 收口时的外部资源事实"
      }
    ]
  },
  "session_ready": {
    "v": 1,
    "terminal": false,
    "open": true,
    "fields": [
      {
        "name": "sessionId",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "sessionFile",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "model",
        "types": [
          "string",
          "null"
        ],
        "required": false
      },
      {
        "name": "thinkingLevel",
        "types": [
          "string",
          "null"
        ],
        "required": false
      }
    ]
  },
  "turn_started": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": []
  },
  "turn_finished": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": []
  },
  "assistant_delta": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "text",
        "types": [
          "string"
        ],
        "required": true
      }
    ]
  },
  "thinking_delta": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "text",
        "types": [
          "string"
        ],
        "required": true
      }
    ]
  },
  "assistant_message": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "text",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "truncated",
        "types": [
          "boolean"
        ],
        "required": false
      },
      {
        "name": "stopReason",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "usage",
        "types": [
          "object"
        ],
        "required": false
      }
    ]
  },
  "deliverables_declared": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "files",
        "types": [
          "array"
        ],
        "required": true,
        "items": "object",
        "doc": "工作区相对路径与可选说明的声明列表"
      }
    ]
  },
  "tool_started": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "toolCallId",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "toolName",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "args",
        "types": [
          "object"
        ],
        "required": true
      },
      {
        "name": "kind",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "presentation",
        "types": [
          "string"
        ],
        "required": false,
        "doc": "族投影的工具呈现提示；缺失时旧帧由读侧回退"
      }
    ]
  },
  "tool_updated": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "toolCallId",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "toolName",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "output",
        "types": [
          "string"
        ],
        "required": true
      }
    ]
  },
  "tool_finished": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "toolCallId",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "toolName",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "isError",
        "types": [
          "boolean"
        ],
        "required": true
      },
      {
        "name": "result",
        "types": [
          "string"
        ],
        "required": true
      }
    ]
  },
  "subagent_started": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "n",
        "types": [
          "integer"
        ],
        "required": true
      },
      {
        "name": "toolCallId",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "agent",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "task",
        "types": [
          "string"
        ],
        "required": true
      }
    ]
  },
  "subagent_finished": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "toolCallId",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "ns",
        "types": [
          "array"
        ],
        "required": true,
        "items": "integer"
      },
      {
        "name": "status",
        "types": [
          "string"
        ],
        "required": true,
        "enum": [
          "finished",
          "failed"
        ]
      },
      {
        "name": "isError",
        "types": [
          "boolean"
        ],
        "required": true
      },
      {
        "name": "resultPreview",
        "types": [
          "string"
        ],
        "required": true
      }
    ]
  },
  "subagent_updated": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "n",
        "types": [
          "integer"
        ],
        "required": true
      },
      {
        "name": "steps",
        "types": [
          "integer"
        ],
        "required": true
      },
      {
        "name": "lastToolName",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "lastToolTarget",
        "types": [
          "string",
          "null"
        ],
        "required": true
      },
      {
        "name": "bytes",
        "types": [
          "integer"
        ],
        "required": true
      }
    ]
  },
  "usage_reported": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "usage",
        "types": [
          "object"
        ],
        "required": true
      },
      {
        "name": "requestUsage",
        "types": [
          "object"
        ],
        "required": false
      }
    ]
  },
  "kernel_status": {
    "v": 1,
    "terminal": false,
    "open": false,
    "fields": [
      {
        "name": "level",
        "types": [
          "string"
        ],
        "required": true,
        "enum": [
          "info",
          "warning"
        ]
      },
      {
        "name": "message",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "detail",
        "types": [
          "string"
        ],
        "required": true
      },
      {
        "name": "extensionPath",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "resumeMethod",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "sessionId",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "replayedUpdates",
        "types": [
          "integer"
        ],
        "required": false
      },
      {
        "name": "decision",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "policy",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "toolCallId",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "toolTitle",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "toolKind",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "toolKindSource",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "optionId",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "optionKind",
        "types": [
          "string"
        ],
        "required": false
      },
      {
        "name": "commands",
        "types": [
          "array"
        ],
        "required": false,
        "items": "object"
      }
    ]
  }
});

/** 全部产品事件类型（SSE 具名帧）。 */
export const EVENT_TYPES = Object.freeze(["run_started", "run_stage", "run_finished", "run_failed", "run_cancelled", "session_ready", "turn_started", "turn_finished", "assistant_delta", "thinking_delta", "assistant_message", "deliverables_declared", "tool_started", "tool_updated", "tool_finished", "subagent_started", "subagent_finished", "subagent_updated", "usage_reported", "kernel_status"]);

/** 每个事件类型的 payload 版本；reducer 只应用 v 与此一致的帧。 */
export const EVENT_VERSIONS = Object.freeze({"run_started": 1, "run_stage": 1, "run_finished": 1, "run_failed": 1, "run_cancelled": 1, "session_ready": 1, "turn_started": 1, "turn_finished": 1, "assistant_delta": 1, "thinking_delta": 1, "assistant_message": 1, "deliverables_declared": 1, "tool_started": 1, "tool_updated": 1, "tool_finished": 1, "subagent_started": 1, "subagent_finished": 1, "subagent_updated": 1, "usage_reported": 1, "kernel_status": 1});

/** 权威终帧类型。 */
export const TERMINAL_EVENT_TYPES = new Set(["run_finished", "run_failed", "run_cancelled"]);
