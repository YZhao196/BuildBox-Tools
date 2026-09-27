// A JSON value, and just enough of a parser and writer to speak the protocol.
//
// Deliberately small. This header exists so the client has no dependency at all
// — a robot build system is a hostile place to introduce one, and the messages
// exchanged here are a handful of known fields rather than arbitrary documents.
// Objects keep insertion order and search linearly, which is the right trade at
// this size and avoids pulling in a map.
//
// It is not a general-purpose JSON library and does not pretend to be: it
// handles what the protocol sends, rejects what is malformed, and has no
// interest in anything else.

#pragma once

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace buildbox {

class JsonError : public std::runtime_error {
 public:
  explicit JsonError(const std::string& what) : std::runtime_error(what) {}
};

class Json {
 public:
  enum class Kind { Null, Bool, Number, String, Array, Object };

  using Array = std::vector<Json>;
  using Member = std::pair<std::string, Json>;
  using Object = std::vector<Member>;

  Json() = default;
  Json(std::nullptr_t) {}
  Json(bool value) : kind_(Kind::Bool), bool_(value) {}
  Json(double value) : kind_(Kind::Number), number_(value) {}
  Json(int value) : kind_(Kind::Number), number_(static_cast<double>(value)) {}
  Json(const char* value) : kind_(Kind::String), string_(value) {}
  Json(std::string value) : kind_(Kind::String), string_(std::move(value)) {}

  static Json array() {
    Json value;
    value.kind_ = Kind::Array;
    return value;
  }
  static Json object() {
    Json value;
    value.kind_ = Kind::Object;
    return value;
  }

  Kind kind() const { return kind_; }
  bool is_null() const { return kind_ == Kind::Null; }
  bool is_object() const { return kind_ == Kind::Object; }
  bool is_array() const { return kind_ == Kind::Array; }
  bool is_string() const { return kind_ == Kind::String; }
  bool is_number() const { return kind_ == Kind::Number; }

  bool as_bool() const { return bool_; }
  double as_number() const { return number_; }
  int as_int() const { return static_cast<int>(number_); }
  const std::string& as_string() const { return string_; }
  const Array& items() const { return array_; }
  const Object& members() const { return object_; }

  // Writable access, so a caller can build a message up field by field.
  void push(Json value) {
    kind_ = Kind::Array;
    array_.push_back(std::move(value));
  }
  void set(std::string key, Json value) {
    kind_ = Kind::Object;
    for (auto& member : object_) {
      if (member.first == key) {
        member.second = std::move(value);
        return;
      }
    }
    object_.emplace_back(std::move(key), std::move(value));
  }

  // A missing member reads as null, so callers can chain without guarding.
  const Json& operator[](const std::string& key) const {
    static const Json empty;
    for (const auto& member : object_) {
      if (member.first == key) return member.second;
    }
    return empty;
  }

  std::string string_or(const std::string& fallback) const {
    return is_string() ? string_ : fallback;
  }
  double number_or(double fallback) const { return is_number() ? number_ : fallback; }
  bool bool_or(bool fallback) const { return kind_ == Kind::Bool ? bool_ : fallback; }

  std::string dump() const {
    std::string out;
    write(out);
    return out;
  }

  static Json parse(const std::string& text) {
    Parser parser(text);
    Json value = parser.value();
    parser.skip_space();
    if (!parser.done()) throw JsonError("Trailing content after the JSON value.");
    return value;
  }

 private:
  void write(std::string& out) const {
    switch (kind_) {
      case Kind::Null:
        out += "null";
        return;
      case Kind::Bool:
        out += bool_ ? "true" : "false";
        return;
      case Kind::Number: {
        if (std::isfinite(number_) && number_ == static_cast<double>(static_cast<long long>(number_))) {
          out += std::to_string(static_cast<long long>(number_));
        } else {
          char buffer[32];
          std::snprintf(buffer, sizeof(buffer), "%.10g", number_);
          out += buffer;
        }
        return;
      }
      case Kind::String:
        write_string(out, string_);
        return;
      case Kind::Array: {
        out += '[';
        for (size_t i = 0; i < array_.size(); ++i) {
          if (i) out += ',';
          array_[i].write(out);
        }
        out += ']';
        return;
      }
      case Kind::Object: {
        out += '{';
        for (size_t i = 0; i < object_.size(); ++i) {
          if (i) out += ',';
          write_string(out, object_[i].first);
          out += ':';
          object_[i].second.write(out);
        }
        out += '}';
        return;
      }
    }
  }

  static void write_string(std::string& out, const std::string& value) {
    out += '"';
    for (unsigned char character : value) {
      switch (character) {
        case '"':
          out += "\\\"";
          break;
        case '\\':
          out += "\\\\";
          break;
        case '\n':
          out += "\\n";
          break;
        case '\r':
          out += "\\r";
          break;
        case '\t':
          out += "\\t";
          break;
        default:
          if (character < 0x20) {
            char buffer[8];
            std::snprintf(buffer, sizeof(buffer), "\\u%04x", character);
            out += buffer;
          } else {
            // Bytes at or above 0x80 are passed through untouched: UTF-8 is
            // already what the wire wants, and re-encoding it here would be a
            // way to corrupt a unit string like "°C".
            out += static_cast<char>(character);
          }
      }
    }
    out += '"';
  }

  class Parser {
   public:
    explicit Parser(const std::string& text) : text_(text) {}

    bool done() const { return at_ >= text_.size(); }

    void skip_space() {
      while (at_ < text_.size()) {
        const char character = text_[at_];
        if (character == ' ' || character == '\t' || character == '\n' || character == '\r') {
          ++at_;
        } else {
          break;
        }
      }
    }

    Json value() {
      skip_space();
      if (done()) throw JsonError("Unexpected end of JSON.");
      const char character = text_[at_];
      switch (character) {
        case '{':
          return object();
        case '[':
          return array();
        case '"':
          return Json(string());
        case 't':
          expect("true");
          return Json(true);
        case 'f':
          expect("false");
          return Json(false);
        case 'n':
          expect("null");
          return Json(nullptr);
        default:
          return number();
      }
    }

   private:
    void expect(const char* literal) {
      const size_t length = std::char_traits<char>::length(literal);
      if (text_.compare(at_, length, literal) != 0) throw JsonError("Malformed JSON literal.");
      at_ += length;
    }

    Json object() {
      ++at_;  // '{'
      Json value = Json::object();
      skip_space();
      if (!done() && text_[at_] == '}') {
        ++at_;
        return value;
      }
      while (true) {
        skip_space();
        std::string key = string();
        skip_space();
        if (done() || text_[at_] != ':') throw JsonError("Expected ':' in an object.");
        ++at_;
        value.set(std::move(key), this->value());
        skip_space();
        if (done()) throw JsonError("Unterminated object.");
        if (text_[at_] == ',') {
          ++at_;
          continue;
        }
        if (text_[at_] == '}') {
          ++at_;
          return value;
        }
        throw JsonError("Expected ',' or '}' in an object.");
      }
    }

    Json array() {
      ++at_;  // '['
      Json value = Json::array();
      skip_space();
      if (!done() && text_[at_] == ']') {
        ++at_;
        return value;
      }
      while (true) {
        value.push(this->value());
        skip_space();
        if (done()) throw JsonError("Unterminated array.");
        if (text_[at_] == ',') {
          ++at_;
          continue;
        }
        if (text_[at_] == ']') {
          ++at_;
          return value;
        }
        throw JsonError("Expected ',' or ']' in an array.");
      }
    }

    std::string string() {
      if (done() || text_[at_] != '"') throw JsonError("Expected a string.");
      ++at_;
      std::string out;
      while (true) {
        if (done()) throw JsonError("Unterminated string.");
        const char character = text_[at_++];
        if (character == '"') return out;
        if (character != '\\') {
          out += character;
          continue;
        }
        if (done()) throw JsonError("Unterminated escape.");
        const char escape = text_[at_++];
        switch (escape) {
          case '"':
            out += '"';
            break;
          case '\\':
            out += '\\';
            break;
          case '/':
            out += '/';
            break;
          case 'b':
            out += '\b';
            break;
          case 'f':
            out += '\f';
            break;
          case 'n':
            out += '\n';
            break;
          case 'r':
            out += '\r';
            break;
          case 't':
            out += '\t';
            break;
          case 'u': {
            if (at_ + 4 > text_.size()) throw JsonError("Truncated \\u escape.");
            unsigned code = 0;
            for (int i = 0; i < 4; ++i) {
              const char digit = text_[at_++];
              code <<= 4;
              if (digit >= '0' && digit <= '9') {
                code |= static_cast<unsigned>(digit - '0');
              } else if (digit >= 'a' && digit <= 'f') {
                code |= static_cast<unsigned>(digit - 'a' + 10);
              } else if (digit >= 'A' && digit <= 'F') {
                code |= static_cast<unsigned>(digit - 'A' + 10);
              } else {
                throw JsonError("Malformed \\u escape.");
              }
            }
            // Re-encoded as UTF-8, including the surrogate pair case, which is
            // how a JSON producer writes anything above the basic plane.
            if (code >= 0xD800 && code <= 0xDBFF && at_ + 6 <= text_.size() && text_[at_] == '\\' &&
                text_[at_ + 1] == 'u') {
              at_ += 2;
              unsigned low = 0;
              for (int i = 0; i < 4; ++i) {
                const char digit = text_[at_++];
                low <<= 4;
                if (digit >= '0' && digit <= '9') {
                  low |= static_cast<unsigned>(digit - '0');
                } else if (digit >= 'a' && digit <= 'f') {
                  low |= static_cast<unsigned>(digit - 'a' + 10);
                } else if (digit >= 'A' && digit <= 'F') {
                  low |= static_cast<unsigned>(digit - 'A' + 10);
                } else {
                  throw JsonError("Malformed \\u escape.");
                }
              }
              code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
            }
            append_utf8(out, code);
            break;
          }
          default:
            throw JsonError("Unknown escape sequence.");
        }
      }
    }

    static void append_utf8(std::string& out, unsigned code) {
      if (code < 0x80) {
        out += static_cast<char>(code);
      } else if (code < 0x800) {
        out += static_cast<char>(0xC0 | (code >> 6));
        out += static_cast<char>(0x80 | (code & 0x3F));
      } else if (code < 0x10000) {
        out += static_cast<char>(0xE0 | (code >> 12));
        out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (code & 0x3F));
      } else {
        out += static_cast<char>(0xF0 | (code >> 18));
        out += static_cast<char>(0x80 | ((code >> 12) & 0x3F));
        out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (code & 0x3F));
      }
    }

    Json number() {
      const size_t start = at_;
      if (!done() && (text_[at_] == '-' || text_[at_] == '+')) ++at_;
      bool any = false;
      while (!done() && ((text_[at_] >= '0' && text_[at_] <= '9') || text_[at_] == '.' ||
                         text_[at_] == 'e' || text_[at_] == 'E' || text_[at_] == '-' ||
                         text_[at_] == '+')) {
        any = true;
        ++at_;
      }
      if (!any) throw JsonError("Expected a number.");
      try {
        return Json(std::stod(text_.substr(start, at_ - start)));
      } catch (const std::exception&) {
        throw JsonError("Malformed number.");
      }
    }

    const std::string& text_;
    size_t at_ = 0;
  };

  Kind kind_ = Kind::Null;
  bool bool_ = false;
  double number_ = 0;
  std::string string_;
  Array array_;
  Object object_;
};

}  // namespace buildbox
