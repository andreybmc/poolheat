package backend

import (
	"context"
	"errors"
	"testing"
)

func TestPriorityCtx(t *testing.T) {
	if IsPriority(context.Background()) {
		t.Fatal("background is not priority")
	}
	ctx := WithPriority(context.Background())
	if !IsPriority(ctx) {
		t.Fatal("WithPriority must set flag")
	}
	if !errors.Is(ErrPreempted, ErrPreempted) {
		t.Fatal("sentinel")
	}
}
