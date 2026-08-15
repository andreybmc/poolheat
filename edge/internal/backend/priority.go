package backend

import (
	"context"
	"errors"
)

type ctxKey int

const priorityKey ctxKey = 1

// ErrPreempted is returned when a background Tuya helper is cancelled
// so a UI command can use the single tinytuya slot.
var ErrPreempted = errors.New("preempted by UI command")

// WithPriority marks a Control() call as a user-facing IPC command.
// Background poll/hold must not use this.
func WithPriority(ctx context.Context) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, priorityKey, true)
}

// IsPriority reports whether ctx is a UI/API command.
func IsPriority(ctx context.Context) bool {
	if ctx == nil {
		return false
	}
	v, _ := ctx.Value(priorityKey).(bool)
	return v
}
