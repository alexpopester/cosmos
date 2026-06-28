# encoding: ascii-8bit

# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE.md for more details.
#
# This file may also be used under the terms of a commercial license
# if purchased from OpenC3, Inc.

require 'openc3/interfaces/interface'
require 'openc3/config/config_parser'
require 'openc3/packets/packet'
require 'openc3/accessors/json_accessor'
require 'faraday'
require 'faraday/follow_redirects'
require 'json'

module OpenC3
  class HttpJsonClientInterface < Interface
    # @param hostname [String] HTTP/HTTPS server to connect to
    # @param port [Integer] HTTP/HTTPS port
    # @param protocol [String] http or https
    # @param write_timeout [Float] Write timeout in seconds
    # @param read_timeout [Float] Read timeout in seconds (nil means no timeout)
    # @param connect_timeout [Float] Connect timeout in seconds
    def initialize(hostname, port = 80, protocol = 'http', write_timeout = 5, read_timeout = nil, connect_timeout = 5)
      super()
      @hostname = hostname
      @port = Integer(port)
      @protocol = protocol
      if (@port == 80 and @protocol == 'http') or (@port == 443 and @protocol == 'https')
        @url = "#{@protocol}://#{@hostname}"
      else
        @url = "#{@protocol}://#{@hostname}:#{@port}"
      end
      @write_timeout = ConfigParser.handle_nil(write_timeout)
      @write_timeout = Float(@write_timeout) if @write_timeout
      @read_timeout = ConfigParser.handle_nil(read_timeout)
      @read_timeout = Float(@read_timeout) if @read_timeout
      @connect_timeout = ConfigParser.handle_nil(connect_timeout)
      @connect_timeout = Float(@connect_timeout) if @connect_timeout
      @response_queue = Queue.new

      # Interface-level configuration
      @headers = {}
      @path = nil
      @http_method = nil
      @response_target_name = nil
      @response_packet_name = nil
      @error_target_name = nil
      @error_packet_name = nil
    end

    def connection_string
      return @url
    end

    # Set an interface-specific option
    # @param option_name [String] Option name
    # @param params [Array] Option values
    def set_option(option_name, params)
      case option_name.upcase
      when 'PATH'
        @path = params[0]
      when 'METHOD'
        @http_method = params[0]
      when 'HEADER'
        @headers[params[0]] = params[1]
      when 'RESPONSE_PACKET'
        @response_target_name = params[0]
        @response_packet_name = params[1]
      when 'ERROR_PACKET'
        @error_target_name = params[0]
        @error_packet_name = params[1]
      else
        super(option_name, params)
      end
    end

    # Connects the interface to its target(s)
    def connect
      request = {}
      request['open_timeout'] = @connect_timeout if @connect_timeout
      request['read_timeout'] = @read_timeout if @read_timeout
      request['write_timeout'] = @write_timeout if @write_timeout
      @http = Faraday.new(request: request) do |f|
        f.response :follow_redirects
        f.adapter :net_http
      end
      super()
    end

    # Whether the interface is connected to its target(s)
    def connected?
      if @http
        return true
      else
        return false
      end
    end

    # Disconnects the interface from its target(s)
    def disconnect
      @http.close if @http
      @http = nil
      while @response_queue.length > 0
        @response_queue.pop
      end
      super()
      @response_queue.push(nil)
    end

    # Called to convert a packet into data.
    # HTTP_* items in the packet override interface-level defaults; all other
    # items are serialized to a flat JSON body.
    #
    # @param packet [Packet] Packet to extract data from
    # @return [Array<String, Hash>] [json_string, extra]
    def convert_packet_to_data(packet)
      fields = {}
      http_path = nil
      http_method = nil
      http_packet = nil
      http_error_packet = nil
      http_queries = {}
      http_headers = {}

      packet.sorted_items.each do |item|
        next if Packet::RESERVED_ITEM_NAMES.include?(item.name)
        case item.name
        when 'HTTP_PATH'
          http_path = packet.read(item.name)
        when 'HTTP_METHOD'
          http_method = packet.read(item.name)
        when 'HTTP_PACKET'
          http_packet = packet.read(item.name)
        when 'HTTP_ERROR_PACKET'
          http_error_packet = packet.read(item.name)
        when /^HTTP_QUERY_/
          http_queries[item.name[11..].downcase] = packet.read(item.name)
        when /^HTTP_HEADER_/
          http_headers[item.name[12..].downcase] = packet.read(item.name)
        else
          fields[item.name] = packet.read(item.name)
        end
      end

      json_string = JSON.generate(fields)

      extra = {}
      extra['HTTP_METHOD'] = http_method || @http_method
      extra['HTTP_HEADERS'] = @headers.merge(http_headers)
      extra['HTTP_QUERIES'] = http_queries unless http_queries.empty?
      extra['HTTP_URI'] = "#{@url}#{http_path || @path}"

      # Per-command response routing — stored in extra so convert_data_to_packet can use it
      if http_packet
        extra['HTTP_PACKET'] = http_packet.upcase
        extra['HTTP_ERROR_PACKET'] = http_error_packet.upcase if http_error_packet
        extra['HTTP_REQUEST_TARGET_NAME'] = packet.target_name
      end

      return json_string, extra
    end

    # Called to convert the read data into a Packet object.
    # Sets JsonAccessor automatically so telemetry definitions don't require it.
    # Per-command routing (HTTP_PACKET in extra) takes priority over interface-level config.
    #
    # @param data [String] Raw JSON response data
    # @param extra [Hash] Contains HTTP_STATUS, HTTP_PACKET, etc.
    # @return [Packet] OpenC3 Packet
    def convert_data_to_packet(data, extra = nil)
      packet = Packet.new(nil, nil, :BIG_ENDIAN, nil, data.to_s)
      packet.accessor = JsonAccessor.new(packet)

      status = extra ? extra['HTTP_STATUS'].to_i : 0

      if extra && extra['HTTP_PACKET']
        target_name = extra['HTTP_REQUEST_TARGET_NAME']
        if status >= 300 && extra['HTTP_ERROR_PACKET']
          packet.target_name = target_name
          packet.packet_name = extra['HTTP_ERROR_PACKET']
        else
          packet.target_name = target_name
          packet.packet_name = extra['HTTP_PACKET']
        end
      elsif status >= 300 && @error_packet_name
        packet.target_name = @error_target_name
        packet.packet_name = @error_packet_name
      else
        packet.target_name = @response_target_name
        packet.packet_name = @response_packet_name
      end

      packet.extra = extra
      return packet
    end

    # Calls the appropriate HTTP method using Faraday
    def write_interface(data, extra = nil)
      extra ||= {}
      queries = extra['HTTP_QUERIES']
      queries ||= {}
      headers = extra['HTTP_HEADERS'] || {}
      uri = extra['HTTP_URI']
      method = extra['HTTP_METHOD']

      resp = nil
      case method
      when 'get'
        resp = @http.get(uri) do |req|
          req.params = queries
          req.headers = headers
        end
      when 'put'
        resp = @http.put(uri) do |req|
          req.params = queries
          req.headers = headers
          req.body = data
        end
      when 'delete'
        resp = @http.delete(uri) do |req|
          req.params = queries
          req.headers = headers
        end
      when 'post'
        resp = @http.post(uri) do |req|
          req.params = queries
          req.headers = headers
          req.body = data
        end
      else
        raise "Unsupported HTTP Method: #{method}"
      end

      response_data = nil
      response_extra = {}
      if resp
        if resp.headers and resp.headers.length > 0
          response_extra['HTTP_HEADERS'] = resp.headers
        end
        response_extra['HTTP_STATUS'] = resp.status
        response_data = resp.body
        response_data ||= ''
      end

      @response_queue.push([response_data, response_extra])

      write_interface_base(data, extra)
      return data, extra
    end

    # Returns response data queued by write_interface
    def read_interface
      data, extra = @response_queue.pop
      return nil if data.nil?

      read_interface_base(data, extra)
      return data, extra
    end
  end
end
