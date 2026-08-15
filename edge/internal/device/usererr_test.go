package device

import (
	"errors"
	"testing"
)

func TestUserFacingError(t *testing.T) {
	cases := []struct {
		err string
		be  string
		want string
	}{
		{"tuya dial 192.168.10.15:6668: dial tcp 192.168.10.15:6668: connect: no route to host", "tuya", "устройство офлайн"},
		{"tuya status: EOF", "tuya", "устройство офлайн"},
		{"tuya status: empty", "tuya", "устройство офлайн"},
		{"connection refused", "ewelink", "устройство офлайн"},
		{"i/o timeout", "tapo", "устройство офлайн"},
		{"local_key empty", "tuya", "ошибка конфигурации"},
		{"devicekey empty", "ewelink", "ошибка конфигурации"},
		{"Tapo email/password empty", "tapo", "ошибка конфигурации"},
		{"backend not supported", "foo", "устройство не поддерживается"},
		{"unknown backend xyz", "", "устройство не поддерживается"},
	}
	for _, c := range cases {
		got := UserFacingError(errors.New(c.err), c.be)
		if got != c.want {
			t.Errorf("UserFacingError(%q, %q) = %q, want %q", c.err, c.be, got, c.want)
		}
	}
}
